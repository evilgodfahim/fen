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

// ===== DECODE NUXT UNICODE ESCAPES =====
// \u002F → /   \u003A → :   (covers both slug and image URL)
function decodeNuxt(str) {
  return str.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) =>
    String.fromCharCode(parseInt(hex, 16))
  );
}

// ===== EXTRACT SLUG → { datetime, image } MAP FROM __NUXT__ =====
// Per-post object layout in the IIFE args:
//   slug:"...", image:"https:\/\/...", caption:..., excerpt:"...", datetime:"ISO"
// image immediately follows slug with no intervening fields.
// datetime is further along (after caption + excerpt) but within ~1500 chars.
function extractNuxtDataMap(html) {
  const map = new Map();

  const scriptMatch = html.match(/window\.__NUXT__\s*=\s*\(function[\s\S]*?;(?=\s*<\/script>)/);
  if (!scriptMatch) {
    console.warn("⚠️  Could not locate __NUXT__ script block — dates/images will fall back");
    return map;
  }

  const script = scriptMatch[0];

  // Capture slug, image, and datetime in one pass per article.
  // image sits right after slug (no other field between them).
  // datetime comes after caption + excerpt (variable length) → 1500 char window.
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

// ===== MAIN =====
async function generateRSS() {
  try {
    const seen     = new Set();
    let   allItems = [];

    for (const url of targetURLs) {
      console.log(`\n--- Processing ${url} ---`);
      const html  = await fetchWithFlareSolverr(url);
      const items = scrapePage(html, seen);
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
        image:       null,
        date:        new Date(),
      });
    }

    const feed = new RSS({
      title:            "The Financial Express – Economy & Trade",
      description:      "Latest Economy and Trade news from The Financial Express Bangladesh",
      feed_url:         baseURL + "/economy",
      site_url:         baseURL,
      language:         "en",
      pubDate:          new Date().toUTCString(),
      // Register the media namespace so <media:content> is valid XML
      custom_namespaces: {
        media: "http://search.yahoo.com/mrss/",
      },
    });

    allItems.forEach(item => {
      const customElements = [];

      if (item.image) {
        // media:content is the standard way feed readers pick up thumbnails
        customElements.push({
          "media:content": {
            _attr: {
              url:    item.image,
              medium: "image",
            },
          },
        });
        // media:thumbnail for readers that prefer this (Feedly, Inoreader, etc.)
        customElements.push({
          "media:thumbnail": {
            _attr: {
              url: item.image,
            },
          },
        });
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
