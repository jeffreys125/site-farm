"""WordPress publishing over the REST API (works for subdomain multisite)."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .config import SiteConfig
from .store import Article, Store


class PublishError(RuntimeError):
    pass


def _resolve_tz(name: str):
    """IANA name -> tzinfo. Unknown names fall back to UTC+8 (China)."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone(timedelta(hours=8))


def schedule_slots(
    count: int,
    at: str = "06:00",
    spacing_minutes: int = 180,
    per_day: int = 3,
    tz_name: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> list[datetime]:
    """Spread `count` posts over future time slots.

    Slots are ``at``, ``at+spacing``, ... on day 0, then the same on day 1,
    and so on. If today's first slot has already passed, the whole schedule
    starts tomorrow. Every returned datetime is strictly in the future, which
    WordPress requires for ``status=future`` posts.
    """
    if count <= 0:
        return []
    tz = _resolve_tz(tz_name)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    try:
        hour, minute = (int(x) for x in at.split(":", 1))
    except (ValueError, AttributeError):
        hour, minute = 6, 0
    hour, minute = min(max(hour, 0), 23), min(max(minute, 0), 59)
    per_day = max(1, per_day)
    spacing_minutes = max(1, spacing_minutes)

    day = now.date()
    first = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    # Always start at least 5 minutes out: a post scheduled for "now" is
    # published immediately by WP-Cron and defeats the point of scheduling.
    if first <= now + timedelta(minutes=5):
        first += timedelta(days=1)

    slots: list[datetime] = []
    for i in range(count):
        day_offset, slot_in_day = divmod(i, per_day)
        slots.append(first + timedelta(days=day_offset, minutes=slot_in_day * spacing_minutes))
    return slots


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
    def publish(
        self,
        article: Article,
        status: str | None = None,
        publish_at: datetime | None = None,
    ) -> tuple[int, str]:
        if not self.site.wp.app_password:
            raise PublishError(
                f"site '{self.site.id}' has no WordPress application password configured"
            )

        category_id = self._ensure_term("categories", self.site.wp.default_category)
        tag_ids = [tid for tid in (self._ensure_term("tags", t) for t in article.tags) if tid]

        effective_status = status or self.site.wp.status
        payload: dict = {
            "title": article.title,
            "slug": article.slug,
            "content": article.body_html,
            "excerpt": article.excerpt,
            "status": effective_status,
            "comment_status": "closed",
            "ping_status": "closed",
            "meta": {"_yoast_wpseo_metadesc": article.meta_desc},
        }
        # WordPress only honours scheduled publishing when status=future AND a
        # date in the future is supplied. date_gmt is the reliable one: it is
        # UTC and independent of the site's own timezone setting.
        if effective_status == "future":
            when = publish_at or schedule_slots(1)[0]
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            payload["date_gmt"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            payload["date"] = when.strftime("%Y-%m-%dT%H:%M:%S")
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
    schedule_at: str = "06:00",
    spacing_minutes: int = 180,
    per_day: int = 3,
    tz_name: str = "Asia/Shanghai",
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

    effective_status = status or site.wp.status
    if effective_status == "future" and not dry_run:
        slots = schedule_slots(len(pending), at=schedule_at, spacing_minutes=spacing_minutes,
                               per_day=per_day, tz_name=tz_name)
        result["scheduled_until"] = slots[-1].strftime("%Y-%m-%d %H:%M %Z") if slots else ""
    else:
        slots = [None] * len(pending)

    publisher = None if dry_run else WordPressPublisher(site)

    for art, slot in zip(pending, slots):
        try:
            if dry_run:
                export_static(art, site, cfg.output_dir / "preview" / site.id)
                result["exported"] += 1
            else:
                assert publisher is not None
                wp_id, link = publisher.publish(art, status=status, publish_at=slot)
                store.mark_published(art.id, wp_id, link)
                result["published"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad post must not stop the batch
            store.mark_failed(art.id, str(exc))
            result["failed"] += 1
            result["errors"].append(f"{art.slug}: {exc}")
    return result
