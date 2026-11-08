"""Compliant material collection.

Only feeds that are explicitly listed in the site config are polled, robots.txt
is honoured, and the license field decides how the text may be reused:

  public-domain  full text may be reused with attribution
  cc-by / cc-by-sa   reuse with attribution + share-alike notice
  reference-only  facts only -- the writer must not reproduce sentences

This module deliberately does NOT crawl arbitrary sites: mass scraping is both a
copyright problem and the single fastest way to get a domain deindexed.
"""

from __future__ import annotations

import re
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import feedparser
import requests
import trafilatura

from .config import Source

UA = (
    "Mozilla/5.0 (compatible; EditorialResearchBot/1.0; "
    "+https://example.com/bot)"
)

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed_by_robots(url: str) -> bool:
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(f"{root}/robots.txt", timeout=15, headers={"User-Agent": UA})
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except requests.RequestException:
            rp = None
        _robots_cache[root] = rp
    rp = _robots_cache[root]
    if rp is None:
        return True  # no robots.txt published
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: str


def read_feed(source: Source, max_items: int = 10) -> list[FeedItem]:
    try:
        raw = requests.get(source.url, timeout=25, headers={"User-Agent": UA})
        raw.raise_for_status()
    except requests.RequestException:
        return []
    parsed = feedparser.parse(raw.content)
    items: list[FeedItem] = []
    for entry in parsed.entries[:max_items]:
        items.append(
            FeedItem(
                title=(entry.get("title") or "").strip(),
                link=(entry.get("link") or "").strip(),
                summary=re.sub(r"<[^>]+>", "", entry.get("summary") or "").strip(),
                published=entry.get("published", "") or "",
            )
        )
    return [i for i in items if i.link and i.title]


def extract_article(url: str, max_chars: int = 6000) -> str:
    """Full-text extraction via trafilatura, respecting robots.txt."""
    if not allowed_by_robots(url):
        return ""
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        r.raise_for_status()
    except requests.RequestException:
        return ""
    text = trafilatura.extract(
        r.text, include_comments=False, include_tables=True, favor_precision=True
    )
    if not text:
        return ""
    return text[:max_chars]


def collect(
    site_id: str, sources: list[Source], per_source: int = 6
) -> tuple[list[dict], dict[str, int]]:
    """Return (materials, per-source counts) ready for Store.add_material().

    A zero count means the feed is dead or blocked -- swap it in the config.
    """
    out: list[dict] = []
    report: dict[str, int] = {}
    for src in sources:
        if src.kind != "rss":
            continue
        items = read_feed(src, max_items=per_source)
        report[src.url] = len(items)
        for item in items:
            body = extract_article(item.link)
            out.append(
                {
                    "source_url": item.link,
                    "source_name": src.name or urlparse(src.url).netloc,
                    "title": item.title,
                    "summary": item.summary,
                    "body": body,
                    "license": src.license,
                }
            )
            time.sleep(1.0)  # be a good citizen
    return out, report
