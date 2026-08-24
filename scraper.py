#!/usr/bin/env python3
"""
The Financial Express (thefinancialexpress.com.bd) scraper.

Outputs in repo root:
  fe_homepage.xml
  fe_today.xml
  fe_editorial_views.xml
  fe_seen_articles.json   ← persists seen URLs across ALL non-today sections;
                              new articles get full content fetched

Requires:
  pip install playwright requests beautifulsoup4 lxml
  playwright install chromium
"""

import email.utils
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.dom import minidom
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser

BASE_URL = "https://thefinancialexpress.com.bd"
TODAY_BASE_URL = "https://today.thefinancialexpress.com.bd"
SEEN_ARTICLES_FILE = Path("fe_seen_articles.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml",
    "Referer": BASE_URL,
}

REQUEST_TIMEOUT = 30
RETRY_DELAYS = [3, 7]
INTER_PAGE_DELAY = 2

# Matches obfuscated email placeholders inserted by Next.js (e.g. "[email protected]")
_EMAIL_OBFUSCATION = re.compile(r"^\[email[\xa0\s]protected\]$")


# ── Utilities ────────────────────────────────────────────────────────────────


def fetch_html(url: str) -> Optional[BeautifulSoup]:
    """Fetch a URL with requests and return BeautifulSoup, with retries.
    Used for non-JS pages (homepage, today, category listings).
    """
    attempts = [0] + RETRY_DELAYS
    for i, delay in enumerate(attempts):
        if delay:
            print(f"  [retry {i}] sleeping {delay}s before re-request ...")
            time.sleep(delay)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except Exception as exc:
            print(f"  [warn] fetch failed ({url}): {exc}")
    print(f"  [error] all attempts exhausted for {url}")
    return None


def fetch_html_playwright(url: str, browser: Browser) -> Optional[BeautifulSoup]:
    """
    Fetch a JS-rendered page using a shared Playwright browser instance.
    Waits for the article body selector before returning.
    """
    page = browser.new_page()
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        # Wait for the article body to appear after JS execution
        try:
            page.wait_for_selector(
                "div[class*='article-body'], div[class*='articleBody'], "
                "div[class*='article_body'], div.article-content",
                timeout=15000,
            )
        except Exception:
            print(f"  [warn] article body selector never appeared: {url}")
            # Still attempt extraction — content may be under a different class
        html = page.content()
    except Exception as exc:
        print(f"  [warn] Playwright fetch failed ({url}): {exc}")
        return None
    finally:
        page.close()

    return BeautifulSoup(html, "lxml")


def clean_text(text: Optional[str]) -> str:
    """Strip whitespace and collapse inner spaces."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def iso_to_rfc822(iso: str) -> str:
    """Convert an ISO 8601 datetime string to RFC-822 format for RSS pubDate."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return email.utils.format_datetime(dt)
    except Exception:
        return email.utils.formatdate(usegmt=True)


def load_seen_urls(path: Path) -> set[str]:
    """Load the set of already-fetched article URLs from disk."""
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen_urls(path: Path, seen: set[str]) -> None:
    """Persist the seen URL set to disk (sorted for readable diffs)."""
    path.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


# ── Article-list scrapers ────────────────────────────────────────────────────


def extract_articles_from_soup(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Extract article cards from a rendered FE page."""
    seen_urls: set[str] = set()
    articles: list[dict] = []

    article_link_re = re.compile(
        r"^https://thefinancialexpress\.com\.bd/"
        r"(?!category/|assets/|_next/|cdn-cgi/|about|contact|terms|privacy|sitemap|epaper|archive)"
        r"[a-z0-9\-]+/[a-z0-9\-]+"
    )

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]

        if href.startswith("/") and not href.startswith("//"):
            href = BASE_URL + href

        if not article_link_re.match(href):
            continue
        if href in seen_urls:
            continue

        heading = ""
        for tag in ("h3", "h2", "h4"):
            h = a_tag.find(tag)
            if h:
                heading = clean_text(h.get_text())
                break

        if not heading:
            parent = a_tag.parent
            for _ in range(4):
                if parent is None:
                    break
                for tag in ("h3", "h2", "h4"):
                    h = parent.find(tag)
                    if h:
                        heading = clean_text(h.get_text())
                        break
                if heading:
                    break
                parent = parent.parent

        if not heading:
            continue

        category = ""
        cat_a = a_tag.find("a", href=re.compile(r"/category/"))
        if not cat_a:
            container = a_tag.parent
            for _ in range(5):
                if container is None:
                    break
                cat_a = container.find("a", href=re.compile(r"/category/"))
                if cat_a:
                    break
                container = container.parent
        if cat_a:
            category = clean_text(cat_a.get_text())

        if not category:
            parts = href.replace(BASE_URL + "/", "").split("/")
            if len(parts) >= 2:
                category = parts[0].replace("-", " ").title()

        pub_time = ""
        container = a_tag.parent
        for _ in range(6):
            if container is None:
                break
            text_nodes = [
                t for t in container.strings
                if re.search(r"\d+\s+(hour|minute|day|month)s?\s+ago|\d{4}-\d{2}-\d{2}", t)
            ]
            if text_nodes:
                pub_time = clean_text(text_nodes[0])
                break
            container = container.parent

        snippet = ""
        container = a_tag.parent
        for _ in range(5):
            if container is None:
                break
            for p in container.find_all("p"):
                txt = clean_text(p.get_text())
                if len(txt) > 40 and txt != heading:
                    snippet = txt[:300]
                    break
            if snippet:
                break
            container = container.parent

        image_url = ""
        img = a_tag.find("img")
        if img:
            src = img.get("src", "")
            m = re.search(r"url=([^&]+)", src)
            if m:
                image_url = unquote(m.group(1))
            elif src.startswith("http"):
                image_url = src

        seen_urls.add(href)
        articles.append(
            {
                "url": href,
                "title": heading,
                "category": category,
                "published": pub_time,
                "snippet": snippet,
                "image": image_url,
            }
        )

    return articles


def extract_articles_from_today_soup(soup: BeautifulSoup) -> list[dict]:
    """Extract articles from today.thefinancialexpress.com.bd."""
    articles: list[dict] = []
    seen_urls: set[str] = set()

    container = soup.find("div", attrs={"itemtype": "https://schema.org/Newspaper"})
    if not container:
        return articles

    current_section = ""

    for row in container.find_all("div", class_="row", recursive=False):
        h2 = row.find("h2", class_="text-center")
        if h2:
            current_section = clean_text(h2.get_text()).title()
            continue

        for post in row.find_all("div", class_="has-post"):
            a_tag = post.find("a", class_="local-news")
            if not a_tag:
                continue

            url = a_tag.get("href", "").strip()
            if not url or url in seen_urls:
                continue

            title = ""
            for h4 in a_tag.find_all("h4"):
                if not h4.find("img"):
                    title = clean_text(h4.get_text())
                    break
            if not title:
                continue

            image_url = ""
            img = post.find("img", class_="img-responsive")
            if img:
                src = img.get("src", "")
                if src.startswith("uploads/"):
                    image_url = f"{TODAY_BASE_URL}/{src}"
                elif src.startswith("http"):
                    image_url = src

            snippet = ""
            col7 = post.find("div", class_="col-lg-7")
            if col7:
                p = col7.find("p")
                if p:
                    snippet = clean_text(p.get_text())
            if not snippet:
                for p in post.find_all("p", recursive=False):
                    txt = clean_text(p.get_text())
                    if txt:
                        snippet = txt
                        break

            seen_urls.add(url)
            articles.append(
                {
                    "url": url,
                    "title": title,
                    "category": current_section,
                    "published": "",
                    "snippet": snippet,
                    "image": image_url,
                }
            )

    return articles


# ── Full-article fetcher ──────────────────────────────────────────────────────


def extract_article_data(soup: BeautifulSoup, url: str) -> dict:
    """
    Extract full body HTML, author, and publication datetime from a
    fully-rendered article soup (post-JS execution).

    Returns a partial article dict with keys:
      full_content  – cleaned HTML string of the article body
      author        – reporter name (empty string if not found)
      pub_iso       – ISO 8601 datetime string (empty string if not found)

    Returns {} if no article body was found at all.
    """
    # ── Author ────────────────────────────────────────────────────────────
    author = ""
    author_a = soup.find("a", href=re.compile(r"^/reporter/"))
    if author_a:
        p = author_a.find("p")
        if p:
            author = clean_text(p.get_text())

    # ── Publication datetime ───────────────────────────────────────────────
    pub_iso = ""
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        pub_iso = time_el["datetime"]

    # ── Article body ──────────────────────────────────────────────────────
    # Try multiple plausible class names for the article body container
    body_div = (
        soup.find("div", class_=lambda c: c and "article-body" in c)
        or soup.find("div", class_=lambda c: c and "articleBody" in c)
        or soup.find("div", class_=lambda c: c and "article_body" in c)
        or soup.find("div", class_=lambda c: c and "article-content" in c)
        or soup.find("div", class_=lambda c: c and "story-content" in c)
    )

    if not body_div:
        print(f"  [warn] no article body div found at {url}")
        return {}

    # Make relative anchor hrefs absolute
    for a in body_div.find_all("a", href=True):
        h = a["href"]
        if h.startswith("/") and not h.startswith("//"):
            a["href"] = BASE_URL + h

    # Unwrap Next.js image proxy URLs; drop layout-only attributes
    for img in body_div.find_all("img"):
        src = img.get("src", "")
        m = re.search(r"url=([^&]+)", src)
        if m:
            img["src"] = unquote(m.group(1))
        elif src.startswith("/"):
            img["src"] = BASE_URL + src
        for attr in ("srcset", "sizes", "loading", "decoding", "fetchpriority"):
            img.attrs.pop(attr, None)

    parts: list[str] = []
    for tag in body_div.find_all(["p", "h2", "h3", "blockquote"]):
        txt = tag.get_text(strip=True)
        if not txt or _EMAIL_OBFUSCATION.match(txt):
            continue
        parts.append(str(tag))

    full_content_html = "\n".join(parts)

    if not full_content_html:
        print(f"  [warn] article body found but empty at {url}")
        return {}

    return {"author": author, "pub_iso": pub_iso, "full_content": full_content_html}


# ── RSS builder ──────────────────────────────────────────────────────────────


def build_rss(title: str, source_urls: list[str], articles: list[dict]) -> str:
    """
    Return a valid RSS 2.0 feed string.

    Articles with a 'full_content' key get a proper CDATA <description> block
    (thumbnail → byline → full body HTML).  Articles without it fall back to
    the existing snippet + image behaviour.
    """
    build_date = email.utils.formatdate(usegmt=True)
    cdata_map: dict[str, str] = {}

    root = ET.Element("rss")
    root.set("version", "2.0")
    root.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    root.set("xmlns:media", "http://search.yahoo.com/mrss/")

    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = source_urls[0] if source_urls else BASE_URL
    ET.SubElement(channel, "description").text = title
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = build_date
    ET.SubElement(channel, "generator").text = "scraper.py"

    for idx, art in enumerate(articles):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = art["title"]
        ET.SubElement(item, "link").text = art["url"]

        guid = ET.SubElement(item, "guid")
        guid.text = art["url"]
        guid.set("isPermaLink", "true")

        full_content = art.get("full_content", "")
        if full_content:
            html_parts: list[str] = []
            if art.get("image"):
                html_parts.append(
                    f'<img src="{art["image"]}"'
                    ' style="max-width:100%;height:auto;display:block;margin-bottom:1em"/>'
                )
            if art.get("author"):
                html_parts.append(f'<p><em>By {art["author"]}</em></p>')
            html_parts.append(full_content)
            html_body = "\n".join(html_parts)
            html_body = html_body.replace("]]>", "]]]]><![CDATA[>")
            key = f"FEPLACEHOLDER{idx}"
            cdata_map[key] = html_body
            ET.SubElement(item, "description").text = key
        else:
            snippet = art.get("snippet") or art["title"]
            if art.get("image"):
                html_body = (
                    f'<img src="{art["image"]}"'
                    ' style="max-width:100%;height:auto;display:block;margin-bottom:0.5em"/>\n'
                    f"<p>{snippet}</p>"
                ).replace("]]>", "]]]]><![CDATA[>")
                key = f"FEPLACEHOLDER{idx}"
                cdata_map[key] = html_body
                ET.SubElement(item, "description").text = key
            else:
                ET.SubElement(item, "description").text = snippet

        pub_iso = art.get("pub_iso", "")
        ET.SubElement(item, "pubDate").text = (
            iso_to_rfc822(pub_iso) if pub_iso else build_date
        )

        if art.get("author"):
            ET.SubElement(item, "author").text = art["author"]

        if art.get("category"):
            ET.SubElement(item, "category").text = art["category"]

        if art.get("image"):
            mc = ET.SubElement(item, "media:content")
            mc.set("url", art["image"])
            mc.set("medium", "image")

    raw = ET.tostring(root, encoding="unicode")
    parsed = minidom.parseString(raw)
    pretty = parsed.toprettyxml(indent="  ", encoding=None)
    body = pretty.split("\n", 1)[1] if "\n" in pretty else pretty
    result = '<?xml version="1.0" encoding="UTF-8"?>\n' + body

    for key, html in cdata_map.items():
        result = result.replace(key, f"<![CDATA[\n{html}\n]]>")

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def scrape_page(url: str) -> list[dict]:
    print(f"Fetching: {url}")
    soup = fetch_html(url)
    if not soup:
        return []
    articles = extract_articles_from_soup(soup, url)
    print(f"  -> {len(articles)} articles found")
    return articles


def scrape_today_page(url: str) -> list[dict]:
    print(f"Fetching: {url}")
    soup = fetch_html(url)
    if not soup:
        return []
    articles = extract_articles_from_today_soup(soup)
    print(f"  -> {len(articles)} articles found")
    return articles


def deduplicate(articles: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            out.append(a)
    return out


def fetch_full_for_new(
    articles: list[dict],
    seen_urls: set[str],
    cap: int,
    label: str,
    browser: Browser,
) -> None:
    """
    In-place: fetch full article content for unseen articles up to *cap* per run.
    Uses a shared Playwright browser instance (one page per article, browser stays open).
    Updates *seen_urls* as articles are processed.
    """
    new = [a for a in articles if a["url"] not in seen_urls][:cap]
    if not new:
        print(f"  No new {label} articles to fetch.")
        return

    for art in new:
        print(f"  [full/{label}] {art['url']}")
        soup = fetch_html_playwright(art["url"], browser)
        if soup:
            extra = extract_article_data(soup, art["url"])
            if extra:
                art.update(extra)
            else:
                print(f"  [warn] extraction returned nothing for {art['url']}")
        else:
            print(f"  [warn] playwright returned no soup for {art['url']}")

        # Mark as seen regardless of success to avoid infinite retries
        seen_urls.add(art["url"])
        time.sleep(INTER_PAGE_DELAY)

    print(f"  Full content fetched for {len(new)} new {label} article(s).")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    seen_urls = load_seen_urls(SEEN_ARTICLES_FILE)

    # Launch one browser for all full-article fetches
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ── Homepage ──────────────────────────────────────────────────────
        home_articles = deduplicate(scrape_page(BASE_URL + "/"))
        fetch_full_for_new(home_articles, seen_urls, cap=15, label="homepage", browser=browser)

        home_path = Path("fe_homepage.xml")
        home_path.write_text(
            build_rss("The Financial Express — Home Page", [BASE_URL + "/"], home_articles),
            encoding="utf-8",
        )
        print(f"\nSaved {len(home_articles)} articles -> {home_path}")

        time.sleep(INTER_PAGE_DELAY)

        # ── Today (snippet only — no full-text fetch) ─────────────────────
        today_articles = deduplicate(scrape_today_page(TODAY_BASE_URL + "/"))
        today_path = Path("fe_today.xml")
        today_path.write_text(
            build_rss(
                "The Financial Express Today — Home Page",
                [TODAY_BASE_URL + "/"],
                today_articles,
            ),
            encoding="utf-8",
        )
        print(f"Saved {len(today_articles)} articles -> {today_path}")

        time.sleep(INTER_PAGE_DELAY)

        # ── Editorial & Views ─────────────────────────────────────────────
        editorial_articles = scrape_page(BASE_URL + "/category/editorial")
        time.sleep(INTER_PAGE_DELAY)
        views_articles = scrape_page(BASE_URL + "/category/views")
        all_scraped = deduplicate(editorial_articles + views_articles)
        fetch_full_for_new(all_scraped, seen_urls, cap=10, label="editorial/views", browser=browser)

        browser.close()

    # Persist once after all sections are processed
    save_seen_urls(SEEN_ARTICLES_FILE, seen_urls)

    ev_path = Path("fe_editorial_views.xml")
    ev_path.write_text(
        build_rss(
            "The Financial Express — Editorial & Views",
            [BASE_URL + "/category/editorial", BASE_URL + "/category/views"],
            all_scraped,
        ),
        encoding="utf-8",
    )
    print(f"Saved {len(all_scraped)} articles -> {ev_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
