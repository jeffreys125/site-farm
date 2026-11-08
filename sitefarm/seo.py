"""Indexing acceleration: sitemap building, IndexNow, and sitemap discovery."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import SiteConfig

UA = "Mozilla/5.0 (compatible; EditorialPipeline/1.0)"


def fetch_published_urls(site: SiteConfig, per_page: int = 100, timeout: int = 45) -> list[dict]:
    """Pull published posts straight from the public REST API (no auth needed)."""
    out: list[dict] = []
    page = 1
    while page <= 20:
        try:
            r = requests.get(
                f"{site.base_url}/wp-json/wp/v2/posts",
                params={"per_page": per_page, "page": page, "_fields": "link,modified,title"},
                timeout=timeout,
                headers={"User-Agent": UA},
            )
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        for item in batch:
            title = ""
            if isinstance(item.get("title"), dict):
                title = re.sub(r"<[^>]+>", "", item["title"].get("rendered", ""))
            out.append({"loc": item["link"], "lastmod": item.get("modified", ""), "title": title})
        if len(batch) < per_page:
            break
        page += 1
    return out


def build_sitemap(site: SiteConfig, urlset_path: Path) -> tuple[Path, int]:
    urls = fetch_published_urls(site)
    NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}urlset")

    def add(loc: str, lastmod: str = "", priority: str = "0.7", changefreq: str = "weekly") -> None:
        u = ET.SubElement(root, f"{{{NS}}}url")
        ET.SubElement(u, f"{{{NS}}}loc").text = loc
        if lastmod:
            try:
                dt = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                ET.SubElement(u, f"{{{NS}}}lastmod").text = dt.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                )
            except ValueError:
                pass
        ET.SubElement(u, f"{{{NS}}}changefreq").text = changefreq
        ET.SubElement(u, f"{{{NS}}}priority").text = priority

    add(site.base_url + "/", priority="1.0", changefreq="daily")
    for item in urls:
        add(item["loc"], item.get("lastmod", ""))

    urlset_path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(urlset_path, encoding="utf-8", xml_declaration=True)
    return urlset_path, len(urls)


def indexnow_key_file(key: str) -> str:
    return key.strip()


def submit_indexnow(host: str, key: str, urls: list[str], key_location: str | None = None) -> dict:
    """Push URLs to Bing / Yandex / Seznam in one call. Google ignores IndexNow."""
    if not key:
        return {"ok": False, "reason": "no INDEXNOW_KEY configured"}
    if not urls:
        return {"ok": False, "reason": "no urls to submit"}

    payload = {
        "host": host,
        "key": key,
        "keyLocation": key_location or f"https://{host}/{key}.txt",
        "urlList": urls[:10000],
    }
    try:
        r = requests.post(
            "https://api.indexnow.org/indexnow",
            json=payload,
            timeout=60,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA},
        )
        return {"ok": r.status_code in (200, 202), "status": r.status_code, "body": r.text[:300]}
    except requests.RequestException as exc:
        return {"ok": False, "reason": str(exc)}


def check_sitemap_live(site: SiteConfig) -> dict:
    """WordPress ships /wp-sitemap.xml since 5.5; Yoast/RankMath use /sitemap_index.xml."""
    candidates = ["/wp-sitemap.xml", "/sitemap_index.xml", "/sitemap.xml"]
    for path in candidates:
        try:
            r = requests.get(site.base_url + path, timeout=25, headers={"User-Agent": UA})
            if r.status_code == 200 and "<sitemap" in r.text[:600].lower():
                return {"ok": True, "url": site.base_url + path}
        except requests.RequestException:
            continue
    return {"ok": False, "hint": "no sitemap found - enable an SEO plugin or upload the generated one"}


def ping_search_engines(sitemap_url: str) -> dict:
    """Bing still honours the deprecated ping endpoint; Google removed it in 2023."""
    results = {}
    for name, endpoint in (
        ("bing", f"https://www.bing.com/ping?sitemap={sitemap_url}"),
    ):
        try:
            r = requests.get(endpoint, timeout=25, headers={"User-Agent": UA})
            results[name] = r.status_code
        except requests.RequestException as exc:
            results[name] = str(exc)
    return results
