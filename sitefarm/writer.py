"""Article generation: brief building, prompt engineering, post-processing."""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any

from .config import ProjectConfig, SiteConfig
from .llm import LLM

BANNED_TOPICS = """
Never write about: politics, elections, religion, adult or sexual content, gambling,
weapons, illegal drugs, medical diagnosis or treatment claims, get-rich-quick
schemes, crypto price predictions, or anything that targets minors.
If a keyword implies one of these, return {"skip": true, "reason": "..."} instead.
""".strip()

SYSTEM_TEMPLATE = """You are a senior staff writer and researcher for {brand}, \
running the {niche} vertical.

Audience: {audience}
Voice: {tone}
Currency: {currency}

You write like a working professional explaining a decision to a smart friend:
concrete numbers, named trade-offs, and the mistake people usually make. No
filler, no "in today's fast-paced world", no rhetorical questions as openers.

Hard rules:
1. Every factual claim must be something a careful editor could verify. Never
   invent statistics, studies, product prices, or quotations.
2. Never reproduce more than 10 consecutive words from any reference material.
   Reference material is provided for FACTS ONLY. Rewrite everything in your own
   structure and phrasing.
3. Prefer specific over general: "a 1/2-inch PEX crimp tool runs $35-$60" beats
   "tools vary in price".
4. Output valid HTML fragments only: <h2>, <h3>, <p>, <ul>, <ol>, <li>, <table>,
   <blockquote>, <strong>, <em>, <a>. No <html>, <head>, <body>, no inline CSS,
   no markdown, no code fences.
5. {banned}
6. Target length: {min_words}-{max_words} words of body copy, excluding FAQ.
"""

BRIEF_TEMPLATE = """Write a complete, publish-ready article for this brief.

SITE: {site_name} ({base_url})
PRIMARY KEYWORD: {keyword}
SEARCH INTENT: {intent}
ARTICLE TYPE: {article_type}

INTERNAL LINK INVENTORY (use 2-4 of these, exact URLs, natural anchor text,
never link the same URL twice):
{link_inventory}

REFERENCE MATERIAL (facts only - do not copy sentences):
{material}

{angle_note}

RECENTLY PUBLISHED ON THIS SITE (do not duplicate or near-duplicate these):
{recent}

Required output - a single JSON object with exactly these keys:
{{
  "skip": false,
  "title": "under 62 characters, front-loads the keyword, no clickbait",
  "slug": "lowercase-hyphenated, 3-6 words",
  "meta_description": "140-158 characters, includes the keyword, promises a concrete benefit",
  "excerpt": "one sentence, under 30 words",
  "tags": ["3-5 short tags"],
  "article_type": "guide|cOmparison|tutorial|explainer|data",
  "body_html": "the full article as an HTML fragment",
  "faq": [{{"q": "question a real reader would type", "a": "2-3 sentence direct answer"}}],
  "key_facts": ["3-6 short factual claims the article makes, for the editor to spot-check"],
  "word_count": 0
}}

Structure the body like this:
- Opening <p>: state the answer or the core trade-off in the first two sentences.
- 3-6 <h2> sections. At least one section must contain a <table> with real,
  comparable values (cost, dimensions, time, capacity - whatever fits).
- At least one <h2> section that covers what goes wrong and how to avoid it.
- Close with a short decision checklist as a <ul>.
- 4-6 FAQ items that are genuinely different from the body headings.
"""


# --------------------------------------------------------------------- brief
def _link_inventory(cfg: ProjectConfig, site: SiteConfig, extra: list[tuple[str, str]] | None = None) -> str:
    lines: list[str] = []
    known = {(t, u) for t, u in (extra or [])}
    for other in cfg.sites:
        if other.id == site.id:
            continue
        if site.siblings and other.id not in site.siblings:
            continue
        label = other.niche.split(",")[0].strip()
        lines.append(f"- {other.base_url}  ->  {label} (sister site, cite only if genuinely relevant)")
    for title, url in list(known)[:8]:
        if url:
            lines.append(f"- {url}  ->  {title} (same site)")
    return "\n".join(lines) if lines else "- (none available yet; write without internal links)"


def _material_block(materials: list[dict]) -> str:
    if not materials:
        return "(none - rely on your own domain knowledge, and keep claims conservative)"
    chunks = []
    for m in materials:
        lic = m.get("license", "reference-only")
        body = (m.get("body") or m.get("summary") or "")[:2500]
        chunks.append(
            f"--- source: {m.get('source_name') or m.get('source_url')} | license: {lic}\n"
            f"title: {m.get('title', '')}\n{body}"
        )
    return "\n\n".join(chunks)


def _article_type_for(keyword: str, intent: str) -> str:
    kw = keyword.lower()
    if any(w in kw for w in ("vs", "versus", "compare", "best ", "top ")):
        return "comparison"
    if kw.startswith(("how to", "how do", "how does")) or intent == "informational":
        return "tutorial"
    if any(w in kw for w in ("cost", "price", "average", "statistics", "data")):
        return "data"
    return "guide"


# ------------------------------------------------------------------ generate
def suggest_keywords(llm: LLM, site: SiteConfig, count: int = 25) -> list[tuple[str, str, int]]:
    system = SYSTEM_TEMPLATE.format(
        brand="the editorial team",
        niche=site.niche,
        audience=site.audience,
        tone=site.tone,
        currency=site.currency,
        min_words=site.min_words,
        max_words=site.max_words,
        banned=BANNED_TOPICS,
    )
    user = f"""Produce {count} distinct long-tail search queries for this site.

Seed themes: {", ".join(site.topic_seeds) or site.niche}

Rules:
- English, lowercase, 4-9 words, the way a real person types into Google.
- 60% informational ("how to ...", "what causes ...", "... cost"), 25% commercial
  ("best ... for ...", "... vs ..."), 15% comparison/definition.
- No brand names unless the brand is the subject of the query.
- No duplicates of each other, no near-duplicates that differ by one word.

Return JSON: {{"keywords": [{{"keyword": "...", "intent": "informational|commercial|transactional|navigational", "priority": 1-10}}]}}"""
    data = llm.json_object(system, user, temperature=0.9)
    out: list[tuple[str, str, int]] = []
    for item in data.get("keywords", []):
        kw = (item.get("keyword") or "").strip().lower()
        if not kw or len(kw.split()) < 3:
            continue
        out.append(
            (
                kw,
                item.get("intent", "informational"),
                int(item.get("priority", 5)),
            )
        )
    return out


def generate_article(
    llm: LLM,
    cfg: ProjectConfig,
    site: SiteConfig,
    keyword: str,
    intent: str = "informational",
    materials: list[dict] | None = None,
    recent_titles: list[str] | None = None,
    internal_links: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    system = SYSTEM_TEMPLATE.format(
        brand=cfg.brand,
        niche=site.niche,
        audience=site.audience,
        tone=site.tone,
        currency=site.currency,
        min_words=site.min_words,
        max_words=site.max_words,
        banned=BANNED_TOPICS,
    )
    article_type = _article_type_for(keyword, intent)
    angle = (
        f"ANGLE: include at least one comparison table with numbers a reader could "
        f"act on, and name the single most common mistake for this task."
    )
    user = BRIEF_TEMPLATE.format(
        site_name=site.niche,
        base_url=site.base_url,
        keyword=keyword,
        intent=intent,
        article_type=article_type,
        link_inventory=_link_inventory(cfg, site, internal_links),
        material=_material_block(materials or []),
        angle_note=angle,
        recent="\n".join(f"- {t}" for t in (recent_titles or [])[:20]) or "- (none yet)",
    )
    data = llm.json_object(system, user)
    if data.get("skip"):
        raise ValueError(f"model declined keyword '{keyword}': {data.get('reason', '')}")
    return postprocess(data, site, keyword)


# -------------------------------------------------------------- postprocess
def postprocess(data: dict, site: SiteConfig, keyword: str) -> dict:
    body = (data.get("body_html") or "").strip()
    body = _sanitize_html(body)
    body = _strip_duplicate_h1(body, data.get("title", ""))

    if site.affiliate_tag:
        body = body.replace("{AFFILIATE_TAG}", site.affiliate_tag)

    disclosure = site.disclosure or (
        "Some links on this page are affiliate links. If you buy through them we may "
        "earn a commission at no extra cost to you. We only recommend gear we would "
        "use ourselves."
    )
    disclosure_html = (
        f'<p class="disclosure"><em>{escape(disclosure)}</em></p>\n'
    )

    faq = [f for f in (data.get("faq") or []) if f.get("q") and f.get("a")]
    faq = faq[:6]
    faq_html = _faq_block(faq)
    schema = _jsonld(site, data, faq, keyword)

    full_body = disclosure_html + body + faq_html + schema

    title = (data.get("title") or keyword.title()).strip()
    slug = _slugify(data.get("slug") or title)
    meta = (data.get("meta_description") or "").strip()
    if not meta:
        meta = f"A practical guide to {keyword}: costs, trade-offs and a checklist you can use today."
    meta = meta[:158]

    return {
        "title": title[:100],
        "slug": slug,
        "meta_desc": meta,
        "excerpt": (data.get("excerpt") or "")[:300],
        "tags": [t for t in (data.get("tags") or []) if t][:5],
        "faq": faq,
        "body_html": full_body,
        "word_count": _word_count(body),
        "keyword": keyword,
        "key_facts": data.get("key_facts", []),
    }


def _sanitize_html(html: str) -> str:
    html = re.sub(r"```(?:html)?", "", html)
    html = re.sub(r"<\s*(script|iframe|style|form|object|embed)\b.*?<\s*/\s*\1\s*>", "", html, flags=re.S | re.I)
    html = re.sub(r"<\s*(script|iframe|style|form|object|embed)\b[^>]*/?>", "", html, flags=re.I)
    html = re.sub(r"\son\w+\s*=\s*\"[^\"]*\"", "", html, flags=re.I)
    html = re.sub(r"\son\w+\s*=\s*'[^']*'", "", html, flags=re.I)
    return html.strip()


def _strip_duplicate_h1(html: str, title: str) -> str:
    m = re.match(r"\s*<h1[^>]*>(.*?)</h1>\s*", html, flags=re.S | re.I)
    if m:
        return html[m.end():]
    return html


def _faq_block(faq: list[dict]) -> str:
    if not faq:
        return ""
    items = "\n".join(
        f"<h3>{escape(f['q'])}</h3>\n<p>{f['a']}</p>" for f in faq
    )
    return f'\n<h2>Frequently asked questions</h2>\n{items}\n'


def _jsonld(site: SiteConfig, data: dict, faq: list[dict], keyword: str) -> str:
    graph: list[dict] = [
        {
            "@type": "Article",
            "headline": data.get("title", "")[:110],
            "description": data.get("meta_description", ""),
            "author": {"@type": "Organization", "name": site.base_url},
            "publisher": {"@type": "Organization", "name": site.base_url},
            "mainEntityOfPage": {"@type": "WebPage", "@id": f"{site.base_url}/"},
        }
    ]
    if faq:
        graph.append(
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": f["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": f["a"]},
                    }
                    for f in faq
                ],
            }
        )
    payload = {"@context": "https://schema.org", "@graph": graph}
    return (
        '\n<script type="application/ld+json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script>"
    )


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    text = re.sub(r"[\s-]+", "-", text)
    words = [w for w in text.split("-") if w]
    stop = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "your", "you"}
    core = [w for w in words if w not in stop][:6]
    return "-".join(core) or "article"


def _word_count(html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", html)
    return len(re.findall(r"\b[\w'-]+\b", text))


# ------------------------------------------------------------------ preview
def to_preview_html(article: dict, site: SiteConfig) -> str:
    faq = "".join(
        f"<details><summary>{escape(f['q'])}</summary><p>{f['a']}</p></details>"
        for f in article.get("faq", [])
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(article['title'])}</title>
<meta name="description" content="{escape(article['meta_desc'])}">
<style>
 body{{max-width:760px;margin:40px auto;padding:0 20px;font:17px/1.7 -apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a}}
 h1{{font-size:32px;line-height:1.25;margin:0 0 12px}}
 h2{{font-size:23px;margin:36px 0 10px}} h3{{font-size:19px;margin:26px 0 8px}}
 table{{border-collapse:collapse;width:100%;margin:18px 0}} th,td{{border:1px solid #ddd;padding:9px 11px;text-align:left}}
 th{{background:#f6f6f6}} .meta{{color:#888;font-size:13px;margin-bottom:24px}}
 .disclosure{{background:#fafafa;border-left:3px solid #ddd;padding:10px 14px;font-size:14px}}
 details{{margin:10px 0}} summary{{cursor:pointer;font-weight:600}}
</style></head><body>
<h1>{escape(article['title'])}</h1>
<div class="meta">{escape(site.base_url)} &middot; {article.get('word_count',0)} words &middot; keyword: {escape(article.get('keyword',''))}</div>
{article['body_html']}
</body></html>"""
