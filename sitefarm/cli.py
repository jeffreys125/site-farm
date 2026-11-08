"""Command line interface for the site-farm pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

from .config import ProjectConfig, SiteConfig, load_config
from .llm import DeepSeek, LLMError
from .publisher import WordPressPublisher, publish_pending
from .seo import build_sitemap, check_sitemap_live, ping_search_engines, submit_indexnow
from .sources import collect
from .store import Store
from .writer import generate_article, suggest_keywords, to_preview_html

STOP = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "your",
    "you", "how", "what", "why", "is", "are", "do", "does", "best", "vs",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2}


def _pick_materials(store: Store, site: SiteConfig, keyword: str, limit: int = 3) -> list[dict]:
    pool = store.take_materials(site.id, limit=40)
    if not pool:
        return []
    kw = _tokens(keyword)
    scored = []
    for m in pool:
        overlap = len(kw & _tokens((m.get("title") or "") + " " + (m.get("summary") or "")))
        if overlap >= 2:
            scored.append((overlap, m))
    scored.sort(key=lambda x: -x[0])
    chosen = [m for _, m in scored[:limit]]
    if chosen:
        store.mark_material_used([m["id"] for m in chosen])
    return chosen


# ---------------------------------------------------------------- commands
def cmd_status(cfg: ProjectConfig, store: Store, args) -> None:
    rows = []
    for site in cfg.sites:
        s = store.stats(site.id)
        k = store.keyword_stats(site.id)
        rows.append(
            {
                "site": site.id,
                "url": site.base_url,
                "articles": s["total"],
                "pending": s["pending"],
                "published": s["published"],
                "materials": store.material_count(site.id),
                "keywords_queued": k["queued"],
            }
        )
    widths = {key: max(len(key), *(len(str(r[key])) for r in rows)) for key in rows[0]}
    header = "  ".join(key.ljust(widths[key]) for key in widths)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[key]).ljust(widths[key]) for key in widths))


def cmd_keywords(cfg: ProjectConfig, store: Store, args) -> None:
    llm = DeepSeek(cfg.deepseek)
    for site in _target_sites(cfg, args):
        keywords = suggest_keywords(llm, site, count=args.count)
        added = store.add_keywords(site.id, keywords)
        print(f"[{site.id}] {added} new keywords queued ({len(keywords)} suggested)")
        for kw, intent, prio in keywords[:8]:
            print(f"    {intent:<14} p{prio}  {kw}")


def cmd_fetch(cfg: ProjectConfig, store: Store, args) -> None:
    for site in _target_sites(cfg, args):
        if not site.sources:
            print(f"[{site.id}] no sources configured, skipping")
            continue
        items, report = collect(site.id, site.sources, per_source=args.per_source)
        added = sum(1 for item in items if store.add_material(site.id, item))
        store.log(site.id, "fetch", f"found={len(items)} new={added}")
        print(f"[{site.id}] fetched {len(items)} items, {added} new, "
              f"{store.material_count(site.id)} unused in pool")
        for url, count in report.items():
            flag = "  OK " if count else " DEAD"
            print(f"    [{flag}] {count:>2} items  {url}")
        if any(c == 0 for c in report.values()):
            print("    -> feeds returning 0 are dead or blocked; replace them in config/sites.yaml")


def cmd_gen(cfg: ProjectConfig, store: Store, args) -> None:
    llm = DeepSeek(cfg.deepseek)
    if cfg.deepseek.mock and not args.mock:
        print("! no DEEPSEEK_API_KEY found - running in mock mode (placeholder copy)", file=sys.stderr)

    for site in _target_sites(cfg, args):
        queue = store.pop_keywords(site.id, limit=args.count)
        if not queue:
            print(f"[{site.id}] keyword queue empty, generating {args.count * 3} seeds first")
            store.add_keywords(site.id, suggest_keywords(llm, site, count=args.count * 3))
            queue = store.pop_keywords(site.id, limit=args.count)
        if not queue:
            print(f"[{site.id}] still empty, skipping")
            continue

        published = store.get_articles(site.id, status="published", limit=30)
        internal = [(a.title, a.wp_url) for a in published if a.wp_url]
        out_dir = cfg.output_dir / "preview" / site.id

        for row in queue:
            kw = row["keyword"]
            for attempt in (1, 2):
                try:
                    article = generate_article(
                        llm,
                        cfg,
                        site,
                        keyword=kw,
                        intent=row.get("intent") or "informational",
                        materials=_pick_materials(store, site, kw),
                        recent_titles=store.recent_titles(site.id),
                        internal_links=internal,
                    )
                except (LLMError, ValueError) as exc:
                    print(f"[{site.id}] FAILED {kw!r}: {exc}")
                    break

                if store.title_exists(site.id, article["title"]):
                    print(f"[{site.id}] duplicate title, retrying: {article['title']}")
                    continue
                slug = article["slug"]
                if store.slug_exists(site.id, slug):
                    slug = f"{slug}-{attempt + 1}"
                    article["slug"] = slug

                article_id = store.add_article(site.id, article)
                if not article_id:
                    print(f"[{site.id}] duplicate body (simhash), retrying")
                    continue

                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{slug}.html").write_text(
                    to_preview_html(article, site), encoding="utf-8"
                )
                print(f"[{site.id}] + {article['title']}  ({article['word_count']}w) -> {slug}")
                break


def cmd_publish(cfg: ProjectConfig, store: Store, args) -> None:
    for site in _target_sites(cfg, args):
        res = publish_pending(
            cfg, site, store,
            limit=args.count,
            dry_run=args.dry_run,
            status=args.wp_status,
        )
        print(
            f"[{site.id}] attempted={res['attempted']} published={res['published']} "
            f"exported={res['exported']} failed={res['failed']}"
        )
        for err in res["errors"][:5]:
            print(f"    ! {err}")


def cmd_submit(cfg: ProjectConfig, store: Store, args) -> None:
    for site in _target_sites(cfg, args):
        path, count = build_sitemap(site, cfg.output_dir / "sitemap" / f"{site.id}.xml")
        print(f"[{site.id}] sitemap written: {path} ({count} posts)")

        live = check_sitemap_live(site)
        print(f"[{site.id}] live sitemap: {live.get('url') or live.get('hint')}")

        if args.urls:
            urls = args.urls
        else:
            urls = [a.wp_url for a in store.get_articles(site.id, status="published", limit=500) if a.wp_url]
        if urls and cfg.indexnow_key:
            res = submit_indexnow(site.host, cfg.indexnow_key, urls)
            print(f"[{site.id}] IndexNow: {res}")
        elif not cfg.indexnow_key:
            print(f"[{site.id}] IndexNow skipped (set INDEXNOW_KEY)")


def cmd_plan(cfg: ProjectConfig, store: Store, args) -> None:
    llm = DeepSeek(cfg.deepseek)
    out_path = cfg.output_dir / "content-plan.csv"
    rows = []
    for site in _target_sites(cfg, args):
        keywords = suggest_keywords(llm, site, count=args.count)
        store.add_keywords(site.id, keywords)
        for kw, intent, prio in keywords:
            rows.append(
                {
                    "site": site.id,
                    "url": site.base_url,
                    "keyword": kw,
                    "intent": intent,
                    "priority": prio,
                    "suggested_week": max(1, 11 - prio),
                    "status": "queued",
                }
            )
    if rows:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"content plan written to {out_path} ({len(rows)} topics)")


def cmd_check(cfg: ProjectConfig, store: Store, args) -> None:
    for site in _target_sites(cfg, args):
        if not site.wp.app_password:
            print(f"[{site.id}] no WP app password set")
            continue
        info = WordPressPublisher(site).health()
        print(f"[{site.id}] {json.dumps(info, ensure_ascii=False)}")


def cmd_serve(cfg: ProjectConfig, store: Store, args) -> None:
    """Tiny static preview server (dev only -- never expose this publicly)."""
    import http.server
    import socketserver

    root = cfg.output_dir / "preview"
    if not root.exists():
        print(f"nothing to serve yet: {root} does not exist")
        return
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(root), **kw
    )
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"preview at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        httpd.serve_forever()


# ------------------------------------------------------------------ shared
def _target_sites(cfg: ProjectConfig, args) -> list[SiteConfig]:
    if getattr(args, "site", None):
        return [cfg.site(args.site)]
    return cfg.sites


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sitefarm",
        description="Editorial pipeline for a network of English niche sites.",
    )
    p.add_argument("--config", default=os.environ.get("SITEFARM_CONFIG", "config/sites.yaml"),
                   help="path to the site config")
    p.add_argument("--mock", action="store_true", help="force mock mode (no API calls)")
    p.add_argument("--site", default="", help="limit to a single site id")

    # shared flags accepted on the subcommand as well, e.g. `gen --site diy`
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--mock", action="store_true", default=argparse.SUPPRESS,
                        help="force mock mode (no API calls)")
    common.add_argument("--site", default=argparse.SUPPRESS, help="limit to a single site id")

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="show per-site counters", parents=[common])
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("keywords", help="generate long-tail keywords into the queue", parents=[common])
    s.add_argument("--count", type=int, default=25)
    s.set_defaults(func=cmd_keywords)

    s = sub.add_parser("plan", help="export a content calendar to CSV", parents=[common])
    s.add_argument("--count", type=int, default=25)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("fetch", help="poll configured feeds into the material pool", parents=[common])
    s.add_argument("--per-source", type=int, default=6)
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("gen", help="generate articles for queued keywords", parents=[common])
    s.add_argument("--count", type=int, default=3)
    s.set_defaults(func=cmd_gen)

    s = sub.add_parser("publish", help="push generated articles to WordPress", parents=[common])
    s.add_argument("--count", type=int, default=20)
    s.add_argument("--dry-run", action="store_true", help="write static preview files instead")
    s.add_argument("--wp-status", choices=["draft", "publish"], default=None)
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("submit", help="build sitemap and push URLs to IndexNow", parents=[common])
    s.add_argument("--urls", nargs="*", default=None)
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("check", help="verify WordPress credentials and endpoints", parents=[common])
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("serve", help="serve static previews on localhost", parents=[common])
    s.add_argument("--port", type=int, default=8099)
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config, mock=args.mock)
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        print("hint: copy config/sites.example.yaml to config/sites.yaml and edit it", file=sys.stderr)
        return 2

    store = Store(cfg.data_dir / "pipeline.db")
    try:
        args.func(cfg, store, args)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
