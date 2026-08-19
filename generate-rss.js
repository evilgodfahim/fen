const fs   = require("fs");
const axios = require("axios");
const cheerio = require("cheerio");
const RSS  = require("rss");

const baseURL         = "https://thefinancialexpress.com.bd";
const targetURLs      = [
  "https://thefinancialexpress.com.bd/category/economy",
  "https://thefinancialexpress.com.bd/category/trade",
  "https://thefinancialexpress.com.bd/category/views",
  "https://thefinancialexpress.com.bd/category/editorial",
];
const flareSolverrURL = process.env.FLARESOLVERR_URL || "http://localhost:8191";
const OUTPUT_FILE     = "./feeds/feed.xml";
const MAX_ITEMS       = 500;

// ─── CONSTANTS ────────────────────────────────────────────────────────────────

// Article path must start with a known section followed by a real slug.
// This is the primary guard against author names / navigation links / CDN links.
const VALID_ARTICLE_PATH =
  /^\/(economy|trade|national|stock|world|views|editorial|business|banking|analysis|opinion)\/[a-z0-9][a-z0-9-]{4,}/i;

// Footer boilerplate that bleeds into article <p> tags — reject these.
const BOILERPLATE = [
  /Published by Syed Nasim Manzur/i,
  /International Publications Limited/i,
  /Topkhana Road.*GPO Box/i,
  /Tejgaon Industrial Area/i,
  /Transcraft Limited/i,
];

fs.mkdirSync("./feeds", { recursive: true });

// ─── DATE PARSING ─────────────────────────────────────────────────────────────
// Returns Date | null.  Never throws, never falls back to now() on its own.
function parseDate(raw) {
  if (!raw) return null;
  const s = String(raw).trim();
  if (!s) return null;

  // "3 minutes ago" / "2 hours ago" / "1 day ago"
  const rel = s.match(/^(\d+)\s+(minute|hour|day)s?\s+ago$/i);
  if (rel) {
    const n  = parseInt(rel[1], 10);
    const ms = /minute/i.test(rel[2]) ? n * 60_000
             : /hour/i.test(rel[2])   ? n * 3_600_000
             :                          n * 86_400_000;
    return new Date(Date.now() - ms);
  }

  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

// ─── NUXT UNICODE DECODE ──────────────────────────────────────────────────────
function decodeNuxt(str) {
  return str.replace(/\\u([0-9a-fA-F]{4})/g, (_, h) =>
    String.fromCharCode(parseInt(h, 16))
  );
}

// ─── EXTRACT NUXT MAPS ────────────────────────────────────────────────────────
// Two independent regex passes so the order of keys inside the payload doesn't
// matter, and the 1 500-char proximity window in the old combined regex no
// longer causes misses.
function extractNuxtMaps(html) {
  const dates  = new Map();
  const images = new Map();

  // Nuxt 2: window.__NUXT__ = (function(...){...}(...))
  let block = (html.match(/window\.__NUXT__\s*=\s*\(function[\s\S]*?;(?=\s*<\/script>)/) || [])[0] || null;

  // Nuxt 3: <script id="__NUXT_DATA__" type="application/json">...</script>
  if (!block) {
    const m3 = html.match(/<script[^>]+id="__NUXT_DATA__"[^>]*>([\s\S]*?)<\/script>/i);
    if (m3) block = m3[1];
  }

  if (!block) {
    console.warn("  ⚠️  No __NUXT__ block found — dates/images will rely on DOM");
    return { dates, images };
  }

  // Pass 1 — slug → datetime
  const dtRe  = /slug:"(\\u002F[^"]+)"[\s\S]{0,4000}?datetime:"(\d{4}-\d{2}-\d{2}T[^"]+)"/g;
  let m;
  while ((m = dtRe.exec(block)) !== null) {
    const slug = decodeNuxt(m[1]);
    if (!dates.has(slug)) dates.set(slug, m[2]);
  }

  // Pass 2 — slug → image
  const imgRe = /slug:"(\\u002F[^"]+)"[\s\S]{0,4000}?image:"(https[^"]+)"/g;
  while ((m = imgRe.exec(block)) !== null) {
    const slug = decodeNuxt(m[1]);
    if (!images.has(slug)) images.set(slug, decodeNuxt(m[2]));
  }

  console.log(`  Nuxt map: ${dates.size} dates, ${images.size} images`);
  return { dates, images };
}

// ─── URL VALIDATION ───────────────────────────────────────────────────────────
function isArticleUrl(href) {
  if (!href) return false;
  if (href.includes("/cdn-cgi/")) return false;   // Cloudflare email-obfuscation links
  if (href.includes("#")) return false;
  let path;
  try { path = href.startsWith("http") ? new URL(href).pathname : href; }
  catch { return false; }
  return VALID_ARTICLE_PATH.test(path);
}

// ─── LOAD EXISTING FEED ───────────────────────────────────────────────────────
function loadExistingItems(filePath) {
  if (!fs.existsSync(filePath)) return [];

  const xml   = fs.readFileSync(filePath, "utf8");
  const items = [];

  for (const block of (xml.match(/<item>[\s\S]*?<\/item>/g) || [])) {
    const get = (tag) => {
      const m = block.match(
        new RegExp(`<${tag}><!\\[CDATA\\[([\\s\\S]*?)\\]\\]><\\/${tag}>|<${tag}>([^<]*)<\\/${tag}>`)
      );
      return m ? (m[1] ?? m[2]) : "";
    };
    const getAttr = (tag, attr) => {
      const m = block.match(new RegExp(`<${tag}[^>]*${attr}="([^"]+)"`));
      return m ? m[1] : null;
    };

    const link = get("link").trim();
    if (!link) continue;

    items.push({
      title:       get("title"),
      link,
      description: get("description"),
      category:    get("category"),
      image:       getAttr("media:content", "url") || getAttr("media:thumbnail", "url") || null,
      date:        parseDate(get("pubDate")) ?? new Date(),
    });
  }

  console.log(`  Loaded ${items.length} existing items`);
  return items;
}

// ─── FLARESOLVERR ─────────────────────────────────────────────────────────────
// maxTimeout raised to 90 s — lets the Nuxt app finish hydrating on slow servers.
// waitForSelector: FlareSolverr v2/Playwright builds hold until articles are in
// the DOM before returning HTML.  v1/Selenium builds ignore it harmlessly.
async function fetchWithFlareSolverr(url) {
  console.log(`  Fetching: ${url}`);
  const res = await axios.post(
    `${flareSolverrURL}/v1`,
    {
      cmd:             "request.get",
      url,
      maxTimeout:      90000,
      waitForSelector: "article h3 a",
    },
    { headers: { "Content-Type": "application/json" }, timeout: 100000 }
  );

  if (res.data?.solution?.response) {
    console.log("  ✅ FlareSolverr OK");
    return res.data.solution.response;
  }
  throw new Error(`FlareSolverr returned no solution for ${url}`);
}

// ─── SCRAPE ONE PAGE ──────────────────────────────────────────────────────────
function scrapePage(html, seen) {
  const $              = cheerio.load(html);
  const { dates, images } = extractNuxtMaps(html);
  const items          = [];

  $("article").each((_, el) => {
    const $a = $(el);

    // ── Title + link ──────────────────────────────────────────────────────────
    const $anchor = $a.find("h3 a, h2 a").first();
    const title   = $anchor.text().trim();
    const href    = $anchor.attr("href");

    // Reject: too short (author names are typically ≤ 20 chars),
    //         or URL doesn't look like a real article.
    if (!title || title.length < 20) return;
    if (!isArticleUrl(href)) return;

    const link = href.startsWith("http") ? href : baseURL + href;
    if (seen.has(link)) return;
    seen.add(link);

    let slug;
    try { slug = href.startsWith("http") ? new URL(href).pathname : href; }
    catch { slug = href; }

    // ── Date — three strategies, first one that works wins ───────────────────

    let date = null;

    // 1. <time datetime="..."> anywhere inside this article card
    const $time = $a.find("time[datetime]").first();
    if ($time.length) date = parseDate($time.attr("datetime"));

    // 2. Visible relative/absolute text in a date-looking element
    if (!date) {
      const txt = $a
        .find("time, .time, .date, .ago, [class*='date'], [class*='time'], [class*='ago']")
        .first()
        .text()
        .trim();
      if (txt) date = parseDate(txt);
    }

    // 3. __NUXT__ datetime keyed by slug
    if (!date && dates.has(slug)) date = parseDate(dates.get(slug));

    if (!date) {
      console.warn(`  ⚠️  No date: ${link.slice(baseURL.length)}`);
      date = new Date();    // last resort — at least the feed stays valid
    }

    // ── Image ─────────────────────────────────────────────────────────────────
    const image = images.get(slug)
      || $a.find("img[src]").first().attr("src")
      || $a.find("img[data-src]").first().attr("data-src")
      || null;

    // ── Description — first <p> that isn't boilerplate ───────────────────────
    let description = "";
    $a.find("p").each((_, p) => {
      if (description) return false;
      const txt = $(p).text().trim();
      if (txt.length > 20 && !BOILERPLATE.some(re => re.test(txt))) {
        description = txt;
      }
    });

    // ── Category ──────────────────────────────────────────────────────────────
    const category = $a
      .find("a")
      .filter((_, a) =>
        /^\/(economy|trade|national|stock|world|views|editorial|business|banking|analysis|opinion)$/.test(
          $(a).attr("href") || ""
        )
      )
      .first()
      .text()
      .trim();

    items.push({ title, link, description, category, image, date });
  });

  console.log(`  → ${items.length} valid articles`);
  return items;
}

// ─── BUILD FEED ───────────────────────────────────────────────────────────────
function buildFeed(items) {
  const feed = new RSS({
    title:             "The Financial Express – Economy, Trade, Views & Editorial",
    description:       "Latest Economy, Trade, Views and Editorial from The Financial Express Bangladesh",
    feed_url:          baseURL,
    site_url:          baseURL,
    language:          "en",
    pubDate:           new Date().toUTCString(),
    custom_namespaces: { media: "http://search.yahoo.com/mrss/" },
  });

  for (const item of items) {
    const custom = [];
    if (item.image) {
      custom.push({ "media:content":   { _attr: { url: item.image, medium: "image" } } });
      custom.push({ "media:thumbnail": { _attr: { url: item.image } } });
    }
    feed.item({
      title:           item.title,
      url:             item.link,
      description:     item.description || undefined,
      categories:      item.category ? [item.category] : undefined,
      date:            item.date,
      custom_elements: custom.length ? custom : undefined,
    });
  }

  return feed.xml({ indent: true });
}

// ─── MAIN ─────────────────────────────────────────────────────────────────────
async function generateRSS() {
  try {
    const seen     = new Set();
    let   newItems = [];

    for (const url of targetURLs) {
      console.log(`\n━━ ${url}`);
      const html  = await fetchWithFlareSolverr(url);
      const items = scrapePage(html, seen);
      newItems.push(...items);
    }

    console.log(`\nScraped: ${newItems.length}`);

    const existing = loadExistingItems(OUTPUT_FILE);
    existing.forEach(e => seen.add(e.link));

    const trulyNew = newItems.filter(n => !existing.some(e => e.link === n.link));
    console.log(`Truly new: ${trulyNew.length}`);

    const merged = [...trulyNew, ...existing].slice(0, MAX_ITEMS);
    console.log(`Feed: ${merged.length} / ${MAX_ITEMS}`);

    if (merged.length === 0) {
      merged.push({
        title: "No articles found", link: baseURL,
        description: "Scraper returned no articles.",
        category: "", image: null, date: new Date(),
      });
    }

    fs.writeFileSync(OUTPUT_FILE, buildFeed(merged));
    console.log(`\n✅ Written ${merged.length} items → ${OUTPUT_FILE}`);

  } catch (err) {
    console.error("❌", err.message);
    if (!fs.existsSync(OUTPUT_FILE)) {
      const feed = new RSS({
        title: "Financial Express (error)", feed_url: baseURL,
        site_url: baseURL, language: "en", pubDate: new Date().toUTCString(),
      });
      feed.item({ title: "Feed generation failed", url: baseURL, date: new Date() });
      fs.writeFileSync(OUTPUT_FILE, feed.xml({ indent: true }));
    } else {
      console.log("⚠️  Keeping existing feed.");
    }
  }
}

generateRSS();
