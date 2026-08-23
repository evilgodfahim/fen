#!/usr/bin/env python3
"""
The Financial Express (thefinancialexpress.com.bd) scraper.

Outputs in repo root:
  fe_homepage.xml
  fe_today.xml
  fe_editorial_views.xml
  fe_seen_editorial.json   ← persists seen editorial/views URLs;
                              new articles get full content fetched
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

BASE_URL = "https://thefinancialexpress.com.bd"
TODAY_BASE_URL = "https://today.thefinancialexpress.com.bd"
SEEN_EDITORIAL_FILE = Path("fe_seen_editorial.json")

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
    """Fetch a URL and return BeautifulSoup, with retries."""
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
        r"(?!category/|assets/|_next/|about|contact|terms|privacy|sitemap|epaper|archive)"
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


# ── Full-article fetcher (editorial/views only) ──────────────────────────────


def fetch_full_article(url: str) -> dict:
    """
    Fetch a single FE article page and extract full body HTML, author, and
    publication datetime.

    Returns a partial article dict with keys:
      full_content  – cleaned HTML string of the article body
      author        – reporter name (empty string if not found)
      pub_iso       – ISO 8601 datetime string (empty string if not found)

    Returns {} on fetch failure so callers can fall back to snippet.
    """
    soup = fetch_html(url)
    if not soup:
        return {}

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
        pub_iso = time_el["datetime"]  # e.g. "2026-08-23T18:26:08.000000Z"

    # ── Article body ──────────────────────────────────────────────────────
    full_content_html = ""
    body_div = soup.find("div", class_=lambda c: c and "article-body" in c)
    if body_div:
        # Make relative anchor hrefs absolute so links work inside RSS readers
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
            # Skip empty tags and Next.js email-obfuscation artifacts
            if not txt or _EMAIL_OBFUSCATION.match(txt):
                continue
            parts.append(str(tag))

        full_content_html = "\n".join(parts)

    return {"author": author, "pub_iso": pub_iso, "full_content": full_content_html}


# ── RSS builder ──────────────────────────────────────────────────────────────


def build_rss(title: str, source_urls: list[str], articles: list[dict]) -> str:
    """
    Return a valid RSS 2.0 feed string.

    Articles with a 'full_content' key get a proper CDATA <description> block
    (thumbnail → byline → full body HTML).  Articles without it fall back to
    the existing snippet + image behaviour.

    CDATA injection works via a two-phase approach:
      1. An alphanumeric placeholder is stored in the ET text node so that ET
         and minidom never see or escape angle brackets.
      2. After toprettyxml(), the placeholder is replaced with the real
         <![CDATA[...]]> block in the final string.
    """
    build_date = email.utils.formatdate(usegmt=True)
    cdata_map: dict[str, str] = {}  # placeholder_key → raw HTML for injection

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
            # Full article: CDATA HTML block (thumbnail → byline → body)
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
            # Escape any sequence that would break out of a CDATA section
            html_body = html_body.replace("]]>", "]]]]><![CDATA[>")
            key = f"FEPLACEHOLDER{idx}"
            cdata_map[key] = html_body
            ET.SubElement(item, "description").text = key
        else:
            # Snippet only — existing behaviour preserved for homepage / today
            desc = art.get("snippet") or art["title"]
            if art.get("image"):
                desc = (
                    f'<![CDATA[<img src="{art["image"]}" style="max-width:100%"/>'
                    f"<br/>{desc}]]>"
                )
            ET.SubElement(item, "description").text = desc

        # pubDate: real datetime when available, build time otherwise
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

    # Phase 2: swap placeholder tokens for real CDATA sections
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


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    # ── Homepage ──────────────────────────────────────────────────────────
    home_articles = deduplicate(scrape_page(BASE_URL + "/"))
    home_path = Path("fe_homepage.xml")
    home_path.write_text(
        build_rss("The Financial Express — Home Page", [BASE_URL + "/"], home_articles),
        encoding="utf-8",
    )
    print(f"\nSaved {len(home_articles)} articles -> {home_path}")

    time.sleep(INTER_PAGE_DELAY)

    # ── Today ─────────────────────────────────────────────────────────────
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

    # ── Editorial & Views — full content for new articles only ────────────
    seen_urls = load_seen_urls(SEEN_EDITORIAL_FILE)

    editorial_articles = scrape_page(BASE_URL + "/category/editorial")
    time.sleep(INTER_PAGE_DELAY)
    views_articles = scrape_page(BASE_URL + "/category/views")
    all_scraped = deduplicate(editorial_articles + views_articles)

    # Only unseen articles, capped at 10 per run
    new_articles = [a for a in all_scraped if a["url"] not in seen_urls][:10]

    for art in new_articles:
        print(f"  [full] {art['url']}")
        extra = fetch_full_article(art["url"])
        if extra:
            art.update(extra)
        seen_urls.add(art["url"])
        time.sleep(INTER_PAGE_DELAY)

    print(f"  Full content fetched for {len(new_articles)} new article(s).")
    save_seen_urls(SEEN_EDITORIAL_FILE, seen_urls)

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
