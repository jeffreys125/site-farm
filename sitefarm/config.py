"""Configuration loading for the site-farm content pipeline."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# LLM provider presets.
#
# A preset only supplies defaults: every value can be overridden from .env via
# LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / LLM_THINKING / LLM_MAX_TOKENS. The
# client speaks OpenAI-compatible /chat/completions, so any gateway works --
# presets just remove the guesswork for the two supported setups.
# ---------------------------------------------------------------------------
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    # Official DeepSeek V3: no reasoning, so no extra request parameters.
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "thinking": "on",
    },
    # Self-hosted New API / one-api. Defaults to thinking=off because reasoning
    # models (dots3-note-prev, Qwen3, ...) otherwise spend the whole
    # max_tokens budget on reasoning_content and return no article body.
    "newapi": {
        "base_url": "",
        "model": "",
        "thinking": "off",
    },
    # Anything else: auto-detects reasoning models from the model name.
    "custom": {
        "base_url": "",
        "model": "",
        "thinking": "auto",
    },
}
DEFAULT_PROVIDER = "deepseek"

# Model-name hints used when LLM_THINKING=auto.
THINKING_MODEL_HINTS = (
    "qwen3", "qwq", "dots3", "glm-4.5", "glm-4.6",
    "kimi-k2-thinking", "-think", "thinking",
)


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return text.strip("-")


@dataclass
class WPConfig:
    user: str = ""
    app_password: str = ""
    status: str = "draft"          # draft | publish | future
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
class LLMConfig:
    provider: str = DEFAULT_PROVIDER
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    thinking: str = "on"           # on | off | auto
    temperature: float = 0.75
    max_tokens: int = 8192
    requests_per_minute: int = 40
    mock: bool = False


# Old name kept so existing imports / third-party scripts keep working.
DeepSeekConfig = LLMConfig


@dataclass
class ProjectConfig:
    brand: str
    contact_email: str
    llm: LLMConfig
    sites: list[SiteConfig]
    data_dir: Path
    output_dir: Path
    indexnow_key: str = ""
    bing_api_key: str = ""
    google_sa_json: str = ""

    @property
    def deepseek(self) -> LLMConfig:
        """Backwards-compatible alias; the field is now provider-agnostic."""
        return self.llm

    def site(self, site_id: str) -> SiteConfig:
        for s in self.sites:
            if s.id == site_id:
                return s
        known = ", ".join(s.id for s in self.sites)
        raise KeyError(f"unknown site '{site_id}' (configured: {known})")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def resolve_llm(project: dict, mock: bool = False) -> LLMConfig:
    """Resolve LLM settings.

    Priority: .env variables > provider preset > config/sites.yaml > built-ins.
    The yaml key is still ``project.deepseek`` for backwards compatibility
    (docker/pipeline-entrypoint.sh guards that path when rewriting URLs).
    """
    ds = project.get("deepseek", {}) or project.get("llm", {}) or {}

    provider = (_env("LLM_PROVIDER") or str(ds.get("provider", "") or "") or DEFAULT_PROVIDER).lower()
    if provider not in PROVIDER_PRESETS:
        provider = "custom"
    preset = PROVIDER_PRESETS[provider]

    api_key = _env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY") or str(ds.get("api_key", "") or "")

    # Only the default preset may fall back to endpoint values in sites.yaml.
    # For any other provider those values would silently point at DeepSeek and
    # hide a missing LLM_BASE_URL / LLM_MODEL.
    ds_endpoint = ds if provider == DEFAULT_PROVIDER else {}
    base_url = (
        _env("LLM_BASE_URL")
        or str(ds_endpoint.get("base_url", "") or "")
        or preset["base_url"]
        or "https://api.deepseek.com"
    ).rstrip("/")
    model = (
        _env("LLM_MODEL")
        or str(ds_endpoint.get("model", "") or "")
        or preset["model"]
        or "deepseek-chat"
    )

    thinking = (_env("LLM_THINKING") or preset["thinking"] or "on").lower()
    if thinking not in ("on", "off", "auto"):
        thinking = preset["thinking"]

    if provider != DEFAULT_PROVIDER and api_key and not _env("LLM_BASE_URL"):
        raise ValueError(
            f"LLM_PROVIDER={provider} 需要同时设置 LLM_BASE_URL（含 /v1）和 "
            "LLM_MODEL，否则会误连 api.deepseek.com"
        )

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        thinking=thinking,
        temperature=float(ds.get("temperature", 0.75)),
        max_tokens=int(_env("LLM_MAX_TOKENS") or ds.get("max_tokens", 8192)),
        requests_per_minute=int(ds.get("requests_per_minute", 40)),
        mock=mock or not bool(api_key),
    )


def load_config(path: str | Path, mock: bool = False) -> ProjectConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    project = raw.get("project", {}) or {}

    paths = project.get("paths", {}) or {}
    data_dir = (PROJECT_ROOT / paths.get("data_dir", "data")).resolve()
    output_dir = (PROJECT_ROOT / paths.get("output_dir", "output")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    llm = resolve_llm(project, mock=mock)

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
        llm=llm,
        sites=sites,
        data_dir=data_dir,
        output_dir=output_dir,
        indexnow_key=_env("INDEXNOW_KEY", project.get("indexnow_key", "") or ""),
        bing_api_key=_env("BING_API_KEY", project.get("bing_api_key", "") or ""),
        google_sa_json=_env("GOOGLE_SA_JSON", project.get("google_sa_json", "") or ""),
    )
