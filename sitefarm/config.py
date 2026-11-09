"""Configuration loading for the site-farm content pipeline."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return text.strip("-")


@dataclass
class WPConfig:
    user: str = ""
    app_password: str = ""
    status: str = "draft"          # draft | publish
    default_category: str = "Guides"


@dataclass
class Source:
    url: str
    kind: str = "rss"
    license: str = "reference-only"  # public-domain | cc-by | cc-by-sa | reference-only
    name: str = ""


@dataclass
class SiteConfig:
    id: str
    domain: str
    base_url: str
    niche: str
    audience: str
    tone: str
    currency: str
    categories: list[str]
    topic_seeds: list[str]
    sources: list[Source]
    wp: WPConfig
    per_day: int = 2
    min_words: int = 1200
    max_words: int = 1800
    affiliate_tag: str = ""
    hub_url: str = ""
    siblings: list[str] = field(default_factory=list)
    disclosure: str = ""
    # Where to reach WordPress when the public origin terminates TLS somewhere
    # else. Inside the compose network the domain name resolves to the web
    # container, which speaks plain HTTP only, so publishing over https://<domain>
    # dies with SSL WRONG_VERSION_NUMBER.
    internal_base: str = ""

    @property
    def host(self) -> str:
        return re.sub(r"^https?://", "", self.base_url).strip("/")


@dataclass
class DeepSeekConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.75
    max_tokens: int = 8192
    requests_per_minute: int = 40
    mock: bool = False


@dataclass
class ProjectConfig:
    brand: str
    contact_email: str
    deepseek: DeepSeekConfig
    sites: list[SiteConfig]
    data_dir: Path
    output_dir: Path
    indexnow_key: str = ""
    bing_api_key: str = ""
    google_sa_json: str = ""

    def site(self, site_id: str) -> SiteConfig:
        for s in self.sites:
            if s.id == site_id:
                return s
        known = ", ".join(s.id for s in self.sites)
        raise KeyError(f"unknown site '{site_id}' (configured: {known})")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_config(path: str | Path, mock: bool = False) -> ProjectConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    project = raw.get("project", {})
    ds = project.get("deepseek", {}) or {}

    paths = project.get("paths", {}) or {}
    data_dir = (PROJECT_ROOT / paths.get("data_dir", "data")).resolve()
    output_dir = (PROJECT_ROOT / paths.get("output_dir", "output")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Any OpenAI-compatible endpoint works, not just DeepSeek: point
    # LLM_BASE_URL at the provider and set LLM_MODEL to the name it expects.
    # The DEEPSEEK_* names keep working, so existing .env files are untouched.
    api_key = _env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY", ds.get("api_key", "") or "")
    base_url = _env("LLM_BASE_URL") or ds.get("base_url", "https://api.deepseek.com")
    model = _env("LLM_MODEL") or ds.get("model", "deepseek-chat")

    deepseek = DeepSeekConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=float(ds.get("temperature", 0.75)),
        max_tokens=int(ds.get("max_tokens", 8192)),
        requests_per_minute=int(ds.get("requests_per_minute", 40)),
        mock=mock or not bool(api_key),
    )

    sites: list[SiteConfig] = []
    for item in raw.get("sites", []) or []:
        wp_raw = item.get("wp", {}) or {}
        wp = WPConfig(
            user=wp_raw.get("user", ""),
            app_password=_env(
                f"WP_APP_PASSWORD_{item['id'].upper()}", wp_raw.get("app_password", "") or ""
            ),
            status=wp_raw.get("status", "draft"),
            default_category=wp_raw.get("default_category", "Guides"),
        )
        sources = [
            Source(
                url=s["url"],
                kind=s.get("kind", "rss"),
                license=s.get("license", "reference-only"),
                name=s.get("name", ""),
            )
            for s in (item.get("sources", []) or [])
        ]
        sites.append(
            SiteConfig(
                id=item["id"],
                domain=item.get("domain", ""),
                base_url=item["base_url"].rstrip("/"),
                niche=item.get("niche", ""),
                audience=item.get("audience", ""),
                tone=item.get("tone", "clear, practical, evidence-led"),
                currency=item.get("currency", "USD"),
                categories=item.get("categories", []) or ["Guides"],
                topic_seeds=item.get("topic_seeds", []) or [],
                sources=sources,
                wp=wp,
                per_day=int(item.get("per_day", 2)),
                min_words=int(item.get("min_words", 1200)),
                max_words=int(item.get("max_words", 1800)),
                affiliate_tag=item.get("affiliate_tag", ""),
                hub_url=item.get("hub_url", ""),
                siblings=item.get("siblings", []) or [],
                disclosure=item.get("disclosure", ""),
                internal_base=_env("WP_INTERNAL_BASE") or item.get("internal_base", "") or "",
            )
        )

    if not sites:
        raise ValueError("no sites defined in config")

    return ProjectConfig(
        brand=project.get("brand", "Editorial Network"),
        contact_email=project.get("contact_email", ""),
        deepseek=deepseek,
        sites=sites,
        data_dir=data_dir,
        output_dir=output_dir,
        indexnow_key=_env("INDEXNOW_KEY", project.get("indexnow_key", "") or ""),
        bing_api_key=_env("BING_API_KEY", project.get("bing_api_key", "") or ""),
        google_sa_json=_env("GOOGLE_SA_JSON", project.get("google_sa_json", "") or ""),
    )
