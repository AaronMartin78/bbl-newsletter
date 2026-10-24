"""Collection: discovery -> relevance filter -> article fetch -> storage.

Idempotent by construction: an article_id already present in the articles
table is skipped (v1 limitation: a later revision of a known article is not
re-fetched; revision capture comes with updated-timestamp handling). Body
text lives on disk under data_dir/articles/<article_id>/<content_hash>.txt;
the DB stores the path.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import duckdb

from . import extract, ids, relevance
from .discover import discover
from .fetch import Fetcher
from .registry import Registry, Source


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class SourceReport:
    source_id: str
    status: str
    items_seen: int = 0
    items_new: int = 0
    bytes: int = 0
    errors: list[str] = field(default_factory=list)


def _known(con: duckdb.DuckDBPyConnection, article_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM articles WHERE article_id = ? LIMIT 1", [article_id]
        ).fetchone()
        is not None
    )


def _store_article(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    source: Source,
    url: str,
    title: Optional[str],
    published_at: Optional[datetime],
    body: str,
    now: datetime,
    stale: bool = False,
) -> bool:
    """Insert one article revision; returns False when the revision exists."""
    aid = ids.article_id(url)
    chash = ids.content_hash(body)
    exists = con.execute(
        "SELECT 1 FROM articles WHERE article_id = ? AND content_hash = ?",
        [aid, chash],
    ).fetchone()
    if exists:
        return False
    body_dir = data_dir / "articles" / aid
    body_dir.mkdir(parents=True, exist_ok=True)
    body_path = body_dir / f"{chash}.txt"
    body_path.write_text(body, encoding="utf-8")
    con.execute(
        """
        INSERT INTO articles (article_id, content_hash, source_id, url, title,
                              published_at, observed_at, language, body_path, stale)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            aid, chash, source.id, ids.canonicalize_url(url),
            title, published_at, now, source.language, str(body_path), stale,
        ],
    )
    return True


def collect_source(
    con: duckdb.DuckDBPyConnection,
    source: Source,
    fetcher: Fetcher,
    data_dir: Path,
    max_articles: int = 20,
    now: Optional[datetime] = None,
) -> SourceReport:
    now = now or _utcnow()
    report = SourceReport(source_id=source.id, status="ok")

    state = con.execute(
        "SELECT etag, last_modified FROM source_state WHERE source_id = ?",
        [source.id],
    ).fetchone() or (None, None)

    disc = discover(source, fetcher, etag=state[0], last_modified=state[1], now=now)
    report.status = disc.status
    report.items_seen = len(disc.items)

    exclude = re.compile(source.exclude_url_pattern) if source.exclude_url_pattern else None
    if disc.status == "ok":
        fetched = 0
        for item in disc.items:
            if fetched >= max_articles:
                break
            if exclude and exclude.search(item.url):
                continue
            if not relevance.is_candidate(source, item.title):
                continue
            if relevance.is_promo(item.title):
                continue
            if _known(con, ids.article_id(item.url)):
                continue

            body = extract.from_feed_body(item.feed_body) if item.feed_body else None
            title = item.title
            published = item.published_at
            if body is None:
                page = fetcher.get(item.url)
                if page.status != 200 or page.content is None:
                    report.errors.append(f"{item.url}: {page.error or page.status}")
                    continue
                report.bytes += len(page.content)
                extracted = extract.from_html(page.content, url=item.url, now=now)
                if extracted is None:
                    report.errors.append(f"{item.url}: extraction_failed")
                    continue
                body = extracted.text
                title = title or extracted.title
                published = published or extracted.published_at
            fetched += 1
            if relevance.is_promo(title):  # title may only exist post-extraction
                continue
            # No date from feed, sitemap or page: age unknown, so the article
            # can never be shown as news. Stored (for the record) but stale,
            # excluded from tagging and stories. Without this, a crawl of a
            # dateless archive floods the edition with months-old backlog
            # (observed 2026-10-18: August club news filled the edition).
            if _store_article(con, data_dir, source, item.url, title,
                              published, body, now,
                              stale=published is None):
                report.items_new += 1

    con.execute(
        """
        UPDATE source_state
        SET etag = coalesce(?, etag),
            last_modified = coalesce(?, last_modified),
            last_run_at = ?,
            last_status = ?
        WHERE source_id = ?
        """,
        [disc.etag, disc.last_modified, now, report.status, source.id],
    )
    return report


def collect_all(
    con: duckdb.DuckDBPyConnection,
    registry: Registry,
    fetcher: Fetcher,
    data_dir: Path,
    now: Optional[datetime] = None,
    progress: Optional[Callable[[SourceReport], None]] = None,
) -> tuple[str, list[SourceReport]]:
    """One collect run over all usable sources, recorded in runs/run_sources."""
    now = now or _utcnow()
    run_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO runs VALUES (?, 'collect', ?, NULL, NULL, NULL)", [run_id, now]
    )
    reports: list[SourceReport] = []
    for source in registry.usable():
        rep = collect_source(
            con, source, fetcher, data_dir,
            max_articles=registry.defaults.max_articles_per_source_per_run,
            now=now,
        )
        reports.append(rep)
        if progress is not None:
            progress(rep)
        con.execute(
            "INSERT INTO run_sources VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                run_id, rep.source_id, None, rep.items_seen, rep.items_new,
                rep.bytes, "; ".join(rep.errors) or None,
            ],
        )
    ok = all(r.status in ("ok", "not_modified") for r in reports)
    con.execute(
        "UPDATE runs SET finished_at = ?, ok = ? WHERE run_id = ?",
        [_utcnow(), ok, run_id],
    )
    return run_id, reports
