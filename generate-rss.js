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

// ===== EXTRACT SLUG → DATETIME MAP FROM __NUXT__ =====
// The page embeds all post data in a window.__NUXT__ IIFE.
// Slugs are unicode-escaped: "\u002Feconomy\u002Fsome-slug"
// Datetimes are inline ISO strings: "2026-03-19T02:29:54.000000Z"
// Field order in each post object: id, title, slug, image, caption, excerpt, datetime, category
function extractNuxtDateMap(html) {
  const map = new Map();

  const scriptMatch = html.match(/window\.__NUXT__\s*=\s*\(function[\s\S]*?;(?=\s*<\/script>)/);
  if (!scriptMatch) {
    console.warn("⚠️  Could not locate __NUXT__ script block — dates will fall back to now()");
    return map;
  }

  const script = scriptMatch[0];

  // Match slug + datetime within the same post object (~1000 chars apart at most).
  // [\s\S]{0,1000}? avoids crossing into the next article while still spanning
  // the image/caption/excerpt fields that sit between slug and datetime.
  const re = /slug:"(\\u002F[^"]+)"[\s\S]{0,1000}?datetime:"(\d{4}-\d{2}-\d{2}T[^"]+)"/g;
  let m;
  while ((m = re.exec(script)) !== null) {
    const slug = m[1].replace(/\\u002F/g, "/");  // decode Unicode escapes → "/"
    map.set(slug, m[2]);
  }

  return map;
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
function scrapePage(html, sourceURL, seen) {
  const $ = cheerio.load(html);
  const dateMap = extractNuxtDateMap(html);
  const items = [];

  $("article").each((_, el) => {
    const $article = $(el);

    // Title + link: h3 > a covers both the lead article and grid cards
    const $titleAnchor = $article.find("h3 a").first();
    const title = $titleAnchor.text().trim();
    const href  = $titleAnchor.attr("href");
    if (!title || !href) return;

    const link = href.startsWith("http") ? href : baseURL + href;
    if (seen.has(link)) return;
    seen.add(link);

    // Slug for date lookup — the path portion only (e.g. "/economy/some-slug")
    const slug = href.startsWith("http") ? new URL(href).pathname : href;
    const rawDate = dateMap.get(slug) || "";

    // Description: the excerpt <p> (last <p> safely works for both layouts)
    const description = $article.find("p").last().text().trim();

    // Category: find the section label link (href is exactly "/economy", "/trade", etc.)
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
      date: parseItemDate(rawDate),
    });
  });

  console.log(`  Scraped ${items.length} articles (dateMap size: ${dateMap.size})`);
  return items;
}

// ===== MAIN =====
async function generateRSS() {
  try {
    const seen     = new Set();
    let   allItems = [];

    for (const url of targetURLs) {
      console.log(`\n--- Processing ${url} ---`);
      const html  = await fetchWithFlareSolverr(url);
      const items = scrapePage(html, url, seen);
      allItems    = allItems.concat(items);
    }

    console.log(`\nTotal unique articles: ${allItems.length}`);

    if (allItems.length === 0) {
      console.log("⚠️  No articles found, inserting placeholder");
      allItems.push({
        title:       "No articles found yet",
        link:        baseURL,
        description: "RSS feed could not scrape any articles.",
        category:    "",
        date:        new Date(),
      });
    }

    const feed = new RSS({
      title:       "The Financial Express – Economy & Trade",
      description: "Latest Economy and Trade news from The Financial Express Bangladesh",
      feed_url:    baseURL + "/economy",
      site_url:    baseURL,
      language:    "en",
      pubDate:     new Date().toUTCString(),
    });

    allItems.forEach(item => {
      feed.item({
        title:       item.title,
        url:         item.link,
        description: item.description || undefined,
        categories:  item.category ? [item.category] : undefined,
        date:        item.date,
      });
    });

    const xml = feed.xml({ indent: true });
    fs.writeFileSync("./feeds/feed.xml", xml);
    console.log(`\n✅ RSS generated with ${allItems.length} items → ./feeds/feed.xml`);

  } catch (err) {
    console.error("❌ Error generating RSS:", err.message);

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
    fs.writeFileSync("./feeds/feed.xml", feed.xml({ indent: true }));
  }
}

generateRSS();
