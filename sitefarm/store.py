"""SQLite-backed state store: article ledger + near-duplicate detection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    keyword       TEXT,
    body_html     TEXT,
    meta_desc     TEXT,
    excerpt       TEXT,
    tags          TEXT,
    faq_json      TEXT,
    simhash       INTEGER,
    word_count    INTEGER,
    status        TEXT DEFAULT 'generated',   -- generated | published | failed
    wp_id         INTEGER,
    wp_url        TEXT,
    source_url    TEXT,
    created_at    REAL,
    published_at  REAL,
    UNIQUE(site, slug)
);
CREATE INDEX IF NOT EXISTS idx_articles_site   ON articles(site);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_hash   ON articles(site, simhash);

CREATE TABLE IF NOT EXISTS materials (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    source_name   TEXT,
    title         TEXT,
    summary       TEXT,
    body          TEXT,
    license       TEXT,
    fetched_at    REAL,
    used          INTEGER DEFAULT 0,
    UNIQUE(site, source_url)
);
CREATE INDEX IF NOT EXISTS idx_materials_site ON materials(site);
CREATE INDEX IF NOT EXISTS idx_materials_used ON materials(site, used);

CREATE TABLE IF NOT EXISTS keyword_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    keyword       TEXT NOT NULL,
    intent        TEXT,
    priority      INTEGER DEFAULT 5,
    status        TEXT DEFAULT 'queued',      -- queued | used | skipped
    created_at    REAL,
    UNIQUE(site, keyword)
);
CREATE INDEX IF NOT EXISTS idx_kw_site ON keyword_queue(site, status);

CREATE TABLE IF NOT EXISTS run_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    site      TEXT,
    action    TEXT,
    detail    TEXT
);
"""


@dataclass
class Article:
    id: int
    site: str
    slug: str
    title: str
    keyword: str
    body_html: str
    meta_desc: str
    excerpt: str
    tags: list[str]
    faq: list[dict]
    word_count: int
    status: str
    wp_id: int | None
    wp_url: str
    source_url: str


class Store:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ log
    def log(self, site: str, action: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO run_log (ts, site, action, detail) VALUES (?,?,?,?)",
            (time.time(), site, action, detail[:2000]),
        )
        self.conn.commit()

    # ------------------------------------------------------------- articles
    def slug_exists(self, site: str, slug: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM articles WHERE site=? AND slug=?", (site, slug)
        )
        return cur.fetchone() is not None

    def title_exists(self, site: str, title: str) -> bool:
        norm = _norm(title)
        for row in self.conn.execute(
            "SELECT title FROM articles WHERE site=?", (site,)
        ):
            if _norm(row["title"]) == norm:
                return True
        return False

    def is_duplicate(self, site: str, simhash_value: int, threshold: int = 3) -> bool:
        """Hamming distance on 64-bit simhash."""
        for row in self.conn.execute(
            "SELECT simhash FROM articles WHERE site=? AND simhash IS NOT NULL", (site,)
        ):
            if _hamming(simhash_value, int(row["simhash"])) <= threshold:
                return True
        return False

    def add_article(self, site: str, payload: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO articles
               (site, slug, title, keyword, body_html, meta_desc, excerpt, tags,
                faq_json, simhash, word_count, status, source_url, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                site,
                payload["slug"],
                payload["title"],
                payload.get("keyword", ""),
                payload["body_html"],
                payload.get("meta_desc", ""),
                payload.get("excerpt", ""),
                json.dumps(payload.get("tags", []), ensure_ascii=False),
                json.dumps(payload.get("faq", []), ensure_ascii=False),
                simhash(payload["body_html"]),
                payload.get("word_count", 0),
                "generated",
                payload.get("source_url", ""),
                time.time(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def get_articles(self, site: str, status: str | None = None, limit: int = 100) -> list[Article]:
        q = "SELECT * FROM articles WHERE site=?"
        args: list = [site]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(q, args):
            out.append(
                Article(
                    id=r["id"],
                    site=r["site"],
                    slug=r["slug"],
                    title=r["title"],
                    keyword=r["keyword"] or "",
                    body_html=r["body_html"] or "",
                    meta_desc=r["meta_desc"] or "",
                    excerpt=r["excerpt"] or "",
                    tags=json.loads(r["tags"] or "[]"),
                    faq=json.loads(r["faq_json"] or "[]"),
                    word_count=r["word_count"] or 0,
                    status=r["status"],
                    wp_id=r["wp_id"],
                    wp_url=r["wp_url"] or "",
                    source_url=r["source_url"] or "",
                )
            )
        return out

    def mark_published(self, article_id: int, wp_id: int, wp_url: str) -> None:
        self.conn.execute(
            "UPDATE articles SET status='published', wp_id=?, wp_url=?, published_at=? WHERE id=?",
            (wp_id, wp_url, time.time(), article_id),
        )
        self.conn.commit()

    def mark_failed(self, article_id: int, reason: str) -> None:
        self.conn.execute("UPDATE articles SET status='failed' WHERE id=?", (article_id,))
        self.conn.commit()
        self.log("", "publish-failed", f"article={article_id} {reason}")

    def recent_titles(self, site: str, limit: int = 40) -> list[str]:
        rows = self.conn.execute(
            "SELECT title FROM articles WHERE site=? ORDER BY id DESC LIMIT ?", (site, limit)
        )
        return [r["title"] for r in rows]

    def stats(self, site: str) -> dict:
        row = self.conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='generated' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS published
               FROM articles WHERE site=?""",
            (site,),
        ).fetchone()
        return {"total": row["total"] or 0, "pending": row["pending"] or 0, "published": row["published"] or 0}

    # ------------------------------------------------------------ materials
    def add_material(self, site: str, item: dict) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO materials
                   (site, source_url, source_name, title, summary, body, license, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    site,
                    item["source_url"],
                    item.get("source_name", ""),
                    item.get("title", ""),
                    item.get("summary", ""),
                    item.get("body", ""),
                    item.get("license", "reference-only"),
                    time.time(),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def take_materials(self, site: str, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM materials WHERE site=? AND used=0 ORDER BY id ASC LIMIT ?",
            (site, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_material_used(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        self.conn.executemany("UPDATE materials SET used=1 WHERE id=?", [(i,) for i in ids])
        self.conn.commit()

    def material_count(self, site: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM materials WHERE site=? AND used=0", (site,)
        ).fetchone()
        return int(row["c"] or 0)

    # -------------------------------------------------------------- keyword
    def add_keywords(self, site: str, keywords: list[tuple[str, str, int]]) -> int:
        added = 0
        for kw, intent, prio in keywords:
            try:
                self.conn.execute(
                    "INSERT INTO keyword_queue (site, keyword, intent, priority, created_at) VALUES (?,?,?,?,?)",
                    (site, kw, intent, prio, time.time()),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass
        self.conn.commit()
        return added

    def pop_keywords(self, site: str, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM keyword_queue
               WHERE site=? AND status='queued'
               ORDER BY priority DESC, id ASC LIMIT ?""",
            (site, limit),
        ).fetchall()
        out = [dict(r) for r in rows]
        if out:
            self.conn.executemany(
                "UPDATE keyword_queue SET status='used' WHERE id=?", [(r["id"],) for r in out]
            )
            self.conn.commit()
        return out

    def keyword_stats(self, site: str) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) queued
               FROM keyword_queue WHERE site=?""",
            (site,),
        ).fetchone()
        return {"total": row["total"] or 0, "queued": row["queued"] or 0}


# ---------------------------------------------------------------- helpers
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def simhash(text: str, bits: int = 64) -> int:
    """Cheap 64-bit simhash over word shingles."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    if not words:
        return 0
    shingles = [" ".join(words[i : i + 3]) for i in range(max(1, len(words) - 2))]
    vector = [0] * bits
    for sh in shingles:
        h = int(hashlib.md5(sh.encode()).hexdigest()[:16], 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out
