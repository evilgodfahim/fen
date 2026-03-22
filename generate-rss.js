const fs = require("fs");
const axios = require("axios");
const cheerio = require("cheerio");
const RSS = require("rss");

const baseURL = "https://thefinancialexpress.com.bd";
const targetURLs = [
  "https://thefinancialexpress.com.bd/economy",
  "https://thefinancialexpress.com.bd/trade",
];
const flareSolverrURL = process.env.FLARESOLVERR_URL || "http://localhost:8191";
const OUTPUT_FILE = "./feeds/feed.xml";
const MAX_ITEMS   = 500;

fs.mkdirSync("./feeds", { recursive: true });

// ===== DATE PARSING =====
function parseItemDate(raw) {
  if (!raw || !raw.trim()) return new Date();

  const trimmed = raw.trim();

  const relMatch = trimmed.match(/^(\d+)\s+(minute|hour|day)s?\s+ago$/i);
  if (relMatch) {
    const n    = parseInt(relMatch[1], 10);
    const unit = relMatch[2].toLowerCase();
    const ms   = unit === "minute" ? n * 60_000
               : unit === "hour"   ? n * 3_600_000
               :                     n * 86_400_000;
    return new Date(Date.now() - ms);
  }

  const d = new Date(trimmed);
  if (!isNaN(d.getTime())) return d;

  console.warn(`⚠️  Could not parse date: "${trimmed}" — using now()`);
  return new Date();
}

// ===== DECODE NUXT UNICODE ESCAPES =====
function decodeNuxt(str) {
  return str.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) =>
    String.fromCharCode(parseInt(hex, 16))
  );
}

// ===== EXTRACT SLUG → { datetime, image } MAP FROM __NUXT__ =====
function extractNuxtDataMap(html) {
  const map = new Map();

  const scriptMatch = html.match(/window\.__NUXT__\s*=\s*\(function[\s\S]*?;(?=\s*<\/script>)/);
  if (!scriptMatch) {
    console.warn("⚠️  Could not locate __NUXT__ script block — dates/images will fall back");
    return map;
  }

  const script = scriptMatch[0];
  const re = /slug:"(\\u002F[^"]+)",image:"(https[^"]+)"[\s\S]{0,1500}?datetime:"(\d{4}-\d{2}-\d{2}T[^"]+)"/g;
  let m;
  while ((m = re.exec(script)) !== null) {
    const slug     = decodeNuxt(m[1]);
    const image    = decodeNuxt(m[2]);
    const datetime = m[3];
    map.set(slug, { datetime, image });
  }

  console.log(`  __NUXT__ data map: ${map.size} entries`);
  return map;
}

// ===== LOAD EXISTING ITEMS FROM XML =====
// Parses the current feed.xml and returns an array of plain objects
// so we can deduplicate and merge with new items.
function loadExistingItems(filePath) {
  if (!fs.existsSync(filePath)) return [];

  const xml = fs.readFileSync(filePath, "utf8");
  const items = [];

  // Extract each <item> block
  const itemBlocks = xml.match(/<item>[\s\S]*?<\/item>/g) || [];

  for (const block of itemBlocks) {
    const get = (tag) => {
      const m = block.match(new RegExp(`<${tag}><!\\[CDATA\\[([\\s\\S]*?)\\]\\]><\\/${tag}>|<${tag}>([^<]*)<\\/${tag}>`));
      return m ? (m[1] !== undefined ? m[1] : m[2]) : "";
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
      date:        parseItemDate(get("pubDate")),
    });
  }

  console.log(`  Loaded ${items.length} existing items from ${filePath}`);
  return items;
}

// ===== FLARESOLVERR =====
async function fetchWithFlareSolverr(url) {
  console.log(`Fetching ${url} via FlareSolverr...`);
  const response = await axios.post(
    `${flareSolverrURL}/v1`,
    { cmd: "request.get", url, maxTimeout: 60000 },
    { headers: { "Content-Type": "application/json" }, timeout: 65000 }
  );
  if (response.data?.solution) {
    console.log("✅ FlareSolverr successfully bypassed protection");
    return response.data.solution.response;
  }
  throw new Error("FlareSolverr did not return a solution");
}

// ===== SCRAPE ONE PAGE =====
function scrapePage(html, seen) {
  const $       = cheerio.load(html);
  const dataMap = extractNuxtDataMap(html);
  const items   = [];

  $("article").each((_, el) => {
    const $article = $(el);

    const $titleAnchor = $article.find("h3 a").first();
    const title = $titleAnchor.text().trim();
    const href  = $titleAnchor.attr("href");
    if (!title || !href) return;

    const link = href.startsWith("http") ? href : baseURL + href;
    if (seen.has(link)) return;
    seen.add(link);

    const slug   = href.startsWith("http") ? new URL(href).pathname : href;
    const meta   = dataMap.get(slug) || {};

    const description = $article.find("p").last().text().trim();

    const category = $article.find("a").filter((_, a) => {
      return /^\/(economy|trade|national|stock|world|views|editorial)$/.test(
        $(a).attr("href") || ""
      );
    }).first().text().trim();

    items.push({
      title,
      link,
      description,
      category,
      image: meta.image || null,
      date:  parseItemDate(meta.datetime || ""),
    });
  });

  console.log(`  Scraped ${items.length} articles`);
  return items;
}

// ===== BUILD XML =====
function buildFeed(items) {
  const feed = new RSS({
    title:             "The Financial Express – Economy & Trade",
    description:       "Latest Economy and Trade news from The Financial Express Bangladesh",
    feed_url:          baseURL + "/economy",
    site_url:          baseURL,
    language:          "en",
    pubDate:           new Date().toUTCString(),
    custom_namespaces: {
      media: "http://search.yahoo.com/mrss/",
    },
  });

  items.forEach(item => {
    const customElements = [];
    if (item.image) {
      customElements.push({ "media:content":   { _attr: { url: item.image, medium: "image" } } });
      customElements.push({ "media:thumbnail": { _attr: { url: item.image } } });
    }

    feed.item({
      title:           item.title,
      url:             item.link,
      description:     item.description || undefined,
      categories:      item.category ? [item.category] : undefined,
      date:            item.date,
      custom_elements: customElements.length ? customElements : undefined,
    });
  });

  return feed.xml({ indent: true });
}

// ===== MAIN =====
async function generateRSS() {
  try {
    // 1. Fetch new articles from all target URLs
    const seen     = new Set();
    let   newItems = [];

    for (const url of targetURLs) {
      console.log(`\n--- Processing ${url} ---`);
      const html  = await fetchWithFlareSolverr(url);
      const items = scrapePage(html, seen);
      newItems    = newItems.concat(items);
    }

    console.log(`\nNew articles scraped: ${newItems.length}`);

    // 2. Load existing items; mark their links as seen so new items can dedup against them
    const existingItems = loadExistingItems(OUTPUT_FILE);
    existingItems.forEach(item => seen.add(item.link));

    // 3. Deduplicate new items against existing (seen was pre-populated above,
    //    but scrapePage already used it — re-filter to be safe)
    const trulyNew = newItems.filter(item => {
      // scrapePage added new links to seen while existing links were NOT yet in seen,
      // so we need an explicit check against existing links here.
      return !existingItems.some(e => e.link === item.link);
    });

    console.log(`Truly new (not in existing feed): ${trulyNew.length}`);

    // 4. Merge: new items first (newest at top), then existing — cap at MAX_ITEMS
    const merged = [...trulyNew, ...existingItems].slice(0, MAX_ITEMS);
    console.log(`Merged feed size: ${merged.length} / ${MAX_ITEMS}`);

    if (merged.length === 0) {
      merged.push({
        title:       "No articles found yet",
        link:        baseURL,
        description: "RSS feed could not scrape any articles.",
        category:    "",
        image:       null,
        date:        new Date(),
      });
    }

    // 5. Write
    fs.writeFileSync(OUTPUT_FILE, buildFeed(merged));
    console.log(`\n✅ RSS written with ${merged.length} items → ${OUTPUT_FILE}`);

  } catch (err) {
    console.error("❌ Error generating RSS:", err.message);

    // Only write fallback if no feed exists yet; don't clobber a good existing feed
    if (!fs.existsSync(OUTPUT_FILE)) {
      const feed = new RSS({
        title:       "The Financial Express (error fallback)",
        description: "RSS feed could not scrape, showing placeholder",
        feed_url:    baseURL,
        site_url:    baseURL,
        language:    "en",
        pubDate:     new Date().toUTCString(),
      });
      feed.item({
        title:       "Feed generation failed",
        url:         baseURL,
        description: "An error occurred during scraping.",
        date:        new Date(),
      });
      fs.writeFileSync(OUTPUT_FILE, feed.xml({ indent: true }));
    } else {
      console.log("⚠️  Keeping existing feed intact due to error.");
    }
  }
}

generateRSS();
