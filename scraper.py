#!/usr/bin/env python3
"""
The Financial Express (thefinancialexpress.com.bd) scraper.

Outputs in repo root:
  fe_homepage.xml
  fe_editorial_views.xml
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.dom import minidom
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://thefinancialexpress.com.bd"

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


def extract_articles_from_soup(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """
    Extract article cards from a rendered FE page.
    """
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


def build_xml(title: str, source_urls: list[str], articles: list[dict]) -> str:
    """Return a pretty-printed XML string."""
    root = ET.Element("feed")
    root.set("xmlns:atom", "http://www.w3.org/2005/Atom")

    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "link").text = BASE_URL
    ET.SubElement(root, "generator").text = "scraper.py"
    ET.SubElement(root, "scraped_at").text = datetime.now(timezone.utc).isoformat()

    sources_el = ET.SubElement(root, "source_urls")
    for u in source_urls:
        ET.SubElement(sources_el, "url").text = u

    ET.SubElement(root, "article_count").text = str(len(articles))

    for art in articles:
        item = ET.SubElement(root, "article")
        ET.SubElement(item, "title").text = art["title"]
        ET.SubElement(item, "url").text = art["url"]
        ET.SubElement(item, "category").text = art["category"]
        ET.SubElement(item, "published").text = art["published"]
        if art["snippet"]:
            ET.SubElement(item, "snippet").text = art["snippet"]
        if art["image"]:
            ET.SubElement(item, "image").text = art["image"]

    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    parsed = minidom.parseString(raw)
    return parsed.toprettyxml(indent="  ", encoding=None)


def scrape_page(url: str) -> list[dict]:
    print(f"Fetching: {url}")
    soup = fetch_html(url)
    if not soup:
        return []
    articles = extract_articles_from_soup(soup, url)
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


def main() -> None:
    home_articles = scrape_page(BASE_URL + "/")
    home_articles = deduplicate(home_articles)

    home_xml = build_xml(
        title="The Financial Express — Home Page",
        source_urls=[BASE_URL + "/"],
        articles=home_articles,
    )
    home_path = Path("fe_homepage.xml")
    home_path.write_text(home_xml, encoding="utf-8")
    print(f"\nSaved {len(home_articles)} articles -> {home_path}")

    time.sleep(INTER_PAGE_DELAY)

    editorial_articles = scrape_page(BASE_URL + "/category/editorial")
    time.sleep(INTER_PAGE_DELAY)

    views_articles = scrape_page(BASE_URL + "/category/views")

    combined = deduplicate(editorial_articles + views_articles)
    combined_xml = build_xml(
        title="The Financial Express — Editorial & Views",
        source_urls=[
            BASE_URL + "/category/editorial",
            BASE_URL + "/category/views",
        ],
        articles=combined,
    )
    ev_path = Path("fe_editorial_views.xml")
    ev_path.write_text(combined_xml, encoding="utf-8")
    print(f"Saved {len(combined)} articles -> {ev_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()