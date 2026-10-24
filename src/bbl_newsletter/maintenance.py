"""Maintenance operations on the pipeline database.

cleanup_listing_articles removes articles that early collector versions
wrongly stored from listing/archive/pagination pages (bug observed live
2026-10-13; discovery now excludes them). Every story touching such an
article is removed whole, together with its summaries and edition items,
so the next cluster run rebuilds clean stories from the remaining articles.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from .discover import _LISTING_PATH, _PAGINATION_QUERY
from .registry import Registry


def _drop_stories(con: duckdb.DuckDBPyConnection, stories: list[str]) -> None:
    if not stories:
        return
    sp = ", ".join("?" for _ in stories)
    con.execute(
        f"DELETE FROM edition_items WHERE summary_id IN "
        f"(SELECT summary_id FROM summaries WHERE story_id IN ({sp}))",
        stories,
    )
    con.execute(f"DELETE FROM summaries WHERE story_id IN ({sp})", stories)
    con.execute(f"DELETE FROM story_articles WHERE story_id IN ({sp})", stories)
    con.execute(f"DELETE FROM stories WHERE story_id IN ({sp})", stories)


def exclude_articles(
    con: duckdb.DuckDBPyConnection, urls: list[str], reason: str | None = None
) -> dict:
    """Editorial kill switch: permanently exclude articles by URL. Tagging and
    story building skip excluded articles, so unlike a plain delete the junk
    cannot be resurrected by the next cluster run. Existing tags, game
    attachments and non-game stories (with summaries and edition items) are
    removed; game stories keep their other members."""
    from datetime import datetime

    report = {"excluded": 0, "stories_removed": 0, "not_found": []}
    for url in urls:
        rows = con.execute(
            "SELECT DISTINCT article_id FROM articles WHERE url = ?", [url]
        ).fetchall()
        if not rows:
            report["not_found"].append(url)
            continue
        for (article_id,) in rows:
            con.execute(
                "INSERT INTO article_exclusions VALUES (?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [article_id, url, reason, datetime.now()],
            )
            stories = [r[0] for r in con.execute(
                """
                SELECT DISTINCT s.story_id FROM stories s
                JOIN story_articles sa USING (story_id)
                WHERE sa.article_id = ? AND s.kind != 'game'
                """,
                [article_id],
            ).fetchall()]
            _drop_stories(con, stories)
            report["stories_removed"] += len(stories)
            for table in ("article_teams", "article_games"):
                con.execute(
                    f"DELETE FROM {table} WHERE article_id = ?", [article_id]
                )
            report["excluded"] += 1
    return report


def retag_articles(con: duckdb.DuckDBPyConnection) -> dict:
    """Remove team tags the CURRENT tagging rules would not create (stale
    tags from before the generic-token fix kept non-basketball BR24 stories
    alive, observed live 2026-10-14), then drop every non-game story that
    contains an article left without any team tag, with its summaries and
    edition items. Valid stories and their summaries are untouched."""
    from .cluster import _article_text, _team_patterns

    patterns = _team_patterns(con)
    valid: set[tuple[str, str]] = set()
    for article_id, title, body_path in con.execute(
        """
        SELECT DISTINCT article_id, title, body_path FROM articles
        WHERE article_id NOT IN (SELECT article_id FROM article_exclusions)
        """
    ).fetchall():
        text = _article_text(title, body_path)
        for team_id, pattern in patterns.items():
            if pattern.search(text):
                valid.add((article_id, team_id))

    stale = [
        (a, t) for a, t in con.execute(
            "SELECT article_id, team_id FROM article_teams"
        ).fetchall() if (a, t) not in valid
    ]
    for article_id, team_id in stale:
        con.execute(
            "DELETE FROM article_teams WHERE article_id = ? AND team_id = ?",
            [article_id, team_id],
        )

    orphans = [r[0] for r in con.execute(
        """
        SELECT DISTINCT s.story_id
        FROM stories s
        JOIN story_articles sa USING (story_id)
        WHERE s.kind != 'game'
          AND NOT EXISTS (SELECT 1 FROM article_teams tag
                          WHERE tag.article_id = sa.article_id)
        """
    ).fetchall()]
    if orphans:
        sp = ", ".join("?" for _ in orphans)
        con.execute(
            f"DELETE FROM edition_items WHERE summary_id IN "
            f"(SELECT summary_id FROM summaries WHERE story_id IN ({sp}))",
            orphans,
        )
        con.execute(f"DELETE FROM summaries WHERE story_id IN ({sp})", orphans)
        con.execute(f"DELETE FROM story_articles WHERE story_id IN ({sp})", orphans)
        con.execute(f"DELETE FROM stories WHERE story_id IN ({sp})", orphans)
    return {"stale_tags_removed": len(stale), "stories_removed": len(orphans)}


def _is_listing_url(url: str, entry_path: str | None) -> bool:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if entry_path is not None and path == entry_path:
        return True
    return bool(
        _PAGINATION_QUERY.search(parts.query or "")
        or _LISTING_PATH.search(parts.path)
    )


def cleanup_listing_articles(
    con: duckdb.DuckDBPyConnection, registry: Registry
) -> dict:
    entry_paths: dict[str, str] = {}
    for source in registry.sources:
        if source.entry_point:
            entry_paths[source.id] = urlsplit(source.entry_point).path.rstrip("/")

    junk: list[str] = []
    for article_id, url, source_id in con.execute(
        "SELECT DISTINCT article_id, url, source_id FROM articles"
    ).fetchall():
        if _is_listing_url(url, entry_paths.get(source_id)):
            junk.append(article_id)

    report = {"articles_removed": len(junk), "stories_removed": 0,
              "summaries_removed": 0}
    if not junk:
        return report

    placeholders = ", ".join("?" for _ in junk)
    stories = [r[0] for r in con.execute(
        f"SELECT DISTINCT story_id FROM story_articles WHERE article_id IN ({placeholders})",
        junk,
    ).fetchall()]
    if stories:
        sp = ", ".join("?" for _ in stories)
        report["summaries_removed"] = con.execute(
            f"SELECT count(*) FROM summaries WHERE story_id IN ({sp})", stories
        ).fetchone()[0]
        con.execute(
            f"DELETE FROM edition_items WHERE summary_id IN "
            f"(SELECT summary_id FROM summaries WHERE story_id IN ({sp}))",
            stories,
        )
        con.execute(f"DELETE FROM summaries WHERE story_id IN ({sp})", stories)
        con.execute(f"DELETE FROM story_articles WHERE story_id IN ({sp})", stories)
        con.execute(f"DELETE FROM stories WHERE story_id IN ({sp})", stories)
        report["stories_removed"] = len(stories)

    for table in ("article_teams", "article_games"):
        con.execute(
            f"DELETE FROM {table} WHERE article_id IN ({placeholders})", junk
        )
    body_paths = [r[0] for r in con.execute(
        f"SELECT body_path FROM articles WHERE article_id IN ({placeholders})", junk
    ).fetchall()]
    con.execute(f"DELETE FROM articles WHERE article_id IN ({placeholders})", junk)
    for bp in body_paths:  # best effort; DB rows are the source of truth
        if bp:
            Path(bp).unlink(missing_ok=True)
    return report
