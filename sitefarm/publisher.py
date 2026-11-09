"""WordPress publishing over the REST API (works for subdomain multisite)."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import SiteConfig
from .store import Article, Store


class PublishError(RuntimeError):
    pass


class WordPressPublisher:
    def __init__(self, site: SiteConfig, timeout: int = 60):
        self.site = site
        self.timeout = timeout
        self.public = site.base_url.rstrip("/")
        # Inside the compose network the site's own domain resolves to the web
        # container (extra_hosts), and that container serves plain HTTP only.
        # Publishing to https://<domain> there fails with SSL
        # WRONG_VERSION_NUMBER, so requests go to the internal origin while the
        # Host header keeps WordPress on the right site. Links come back from
        # WordPress itself, so they stay on the public https origin.
        self.base = (site.internal_base or site.base_url).rstrip("/")
        self.session = requests.Session()
        token = base64.b64encode(
            f"{site.wp.user}:{site.wp.app_password}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": "EditorialPipeline/1.0",
        }
        if site.internal_base:
            headers["Host"] = urlparse(self.public).netloc
        self.session.headers.update(headers)
        self._terms: dict[tuple[str, str], int] = {}

    # --------------------------------------------------------------- checks
    def health(self) -> dict:
        r = self.session.get(f"{self.base}/wp-json/wp/v2/users/me", timeout=self.timeout)
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "user": data.get("name"), "id": data.get("id")}
        return {"ok": False, "status": r.status_code, "body": r.text[:300],
                "hint": "check Application Password and that Apache forwards the Authorization header"}

    # ---------------------------------------------------------------- terms
    def _ensure_term(self, taxonomy: str, name: str) -> int | None:
        key = (taxonomy, name.lower())
        if key in self._terms:
            return self._terms[key]
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        try:
            r = self.session.get(
                f"{self.base}/wp-json/wp/v2/{taxonomy}",
                params={"slug": slug},
                timeout=self.timeout,
            )
            if r.status_code == 200 and r.json():
                term_id = int(r.json()[0]["id"])
                self._terms[key] = term_id
                return term_id
            r = self.session.post(
                f"{self.base}/wp-json/wp/v2/{taxonomy}",
                json={"name": name, "slug": slug},
                timeout=self.timeout,
            )
            if r.status_code in (200, 201):
                term_id = int(r.json()["id"])
                self._terms[key] = term_id
                return term_id
        except (requests.RequestException, ValueError, KeyError):
            pass
        return None

    # -------------------------------------------------------------- publish
    def publish(self, article: Article, status: str | None = None) -> tuple[int, str]:
        if not self.site.wp.app_password:
            raise PublishError(
                f"site '{self.site.id}' has no WordPress application password configured"
            )

        category_id = self._ensure_term("categories", self.site.wp.default_category)
        tag_ids = [tid for tid in (self._ensure_term("tags", t) for t in article.tags) if tid]

        payload: dict = {
            "title": article.title,
            "slug": article.slug,
            "content": article.body_html,
            "excerpt": article.excerpt,
            "status": status or self.site.wp.status,
            "comment_status": "closed",
            "ping_status": "closed",
            "meta": {"_yoast_wpseo_metadesc": article.meta_desc},
        }
        if category_id:
            payload["categories"] = [category_id]
        if tag_ids:
            payload["tags"] = tag_ids

        r = self.session.post(
            f"{self.base}/wp-json/wp/v2/posts", json=payload, timeout=self.timeout
        )
        if r.status_code not in (200, 201):
            raise PublishError(f"HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        wp_id = int(data["id"])
        link = data.get("link", "")

        # Some SEO plugins only accept the meta description through their own key.
        if data.get("meta") is not None and "_yoast_wpseo_metadesc" not in (data.get("meta") or {}):
            try:
                self.session.post(
                    f"{self.base}/wp-json/wp/v2/posts/{wp_id}",
                    json={"meta": {"rank_math_description": article.meta_desc}},
                    timeout=self.timeout,
                )
            except requests.RequestException:
                pass
        return wp_id, link


def export_static(article: Article, site: SiteConfig, out_dir: Path) -> Path:
    """Dry-run fallback: write the article as a static file for manual review."""
    from .writer import to_preview_html  # local import to avoid a cycle

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{article.slug}.html"
    path.write_text(
        to_preview_html(
            {
                "title": article.title,
                "meta_desc": article.meta_desc,
                "body_html": article.body_html,
                "word_count": article.word_count,
                "keyword": article.keyword,
                "faq": article.faq,
            },
            site,
        ),
        encoding="utf-8",
    )
    return path


def publish_pending(
    cfg,
    site: SiteConfig,
    store: Store,
    limit: int = 20,
    dry_run: bool = False,
    status: str | None = None,
) -> dict:
    pending = store.get_articles(site.id, status="generated", limit=limit)
    # mark_failed moves an article out of 'generated', so a transient failure
    # (network hiccup, 502 while Apache reloads, expired token) used to strand
    # it forever -- it could never be picked up again. Retry them.
    if len(pending) < limit:
        pending += store.get_articles(
            site.id, status="failed", limit=limit - len(pending)
        )
    result = {"attempted": len(pending), "published": 0, "exported": 0, "failed": 0, "errors": []}
    if not pending:
        return result

    publisher = None if dry_run else WordPressPublisher(site)

    for art in pending:
        try:
            if dry_run:
                export_static(art, site, cfg.output_dir / "preview" / site.id)
                result["exported"] += 1
            else:
                assert publisher is not None
                wp_id, link = publisher.publish(art, status=status)
                store.mark_published(art.id, wp_id, link)
                result["published"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad post must not stop the batch
            store.mark_failed(art.id, str(exc))
            result["failed"] += 1
            result["errors"].append(f"{art.slug}: {exc}")
    return result
