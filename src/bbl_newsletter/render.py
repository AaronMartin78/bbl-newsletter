"""Edition renderer: validated summaries -> one Quarto post per day.

The renderer is deterministic and model-free. It publishes only summaries
whose status is candidate or approved, orders game stories by tipoff, and
builds every citation link from the database (story members' stored URLs),
never from model output. Writing an edition records editions/edition_items;
re-rendering the same date produces identical content and no duplicate rows.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb

SECTION_ORDER = ["games", "euroleague", "team_news"]
SECTION_TITLES = {
    "games": "Games",
    "euroleague": "European Cups",
    "team_news": "Around the League",
}
KIND_TO_SECTION = {"game": "games", "euroleague": "euroleague",
                   "team_news": "team_news", "league": "team_news"}
PUBLISHABLE = ("candidate", "approved")


def _edition_rows(con: duckdb.DuckDBPyConnection, edition_date: date):
    """Publishable summaries for the edition: stories whose newest
    publishable summary is from the edition day or the day before, the
    latest summary per story, and one story per distinct member set so
    re-clustered duplicates of the same event never appear twice. Keying
    the window to the summary rather than the story means a stable game
    story re-lists exactly when it gained sources (its input changed, so a
    fresh summary exists), and a retried reject surfaces once it passes.
    A summary whose input_hash an earlier edition already printed is
    excluded, so a story repeats only when it grew (new sources change the
    input hash); without this, every story re-listed for 2-3 days until it
    aged out of the window (observed 2026-10-21: 12 of 25 items repeated
    the previous edition). The key is the input hash, not the summary or
    story id (re-clustering an unchanged story under the next date mints
    new ones) and not the live member set (a stable game story's membership
    grows, which would retroactively match its updated composite).
    Re-rendering the same edition keeps its own items."""
    return con.execute(
        """
        WITH story_members AS (
            SELECT story_id,
                   string_agg(article_id, ',' ORDER BY article_id) AS members_key
            FROM story_articles GROUP BY story_id
        )
        SELECT su.summary_id, st.story_id, st.kind, st.game_uid,
               su.text_en, g.tipoff, g.matchday,
               EXISTS (
                   SELECT 1 FROM edition_items ei2
                   JOIN summaries su3 USING (summary_id)
                   WHERE su3.story_id = st.story_id AND ei2.edition_date < ?
               ) AS updated
        FROM summaries su
        JOIN stories st USING (story_id)
        JOIN story_members sm USING (story_id)
        LEFT JOIN games g ON g.game_uid = st.game_uid
        WHERE su.status IN (?, ?)
          AND CAST(su.created_at AS DATE)
              BETWEEN ? - INTERVAL 1 DAY AND ?
          AND su.input_hash NOT IN (
              SELECT su2.input_hash
              FROM edition_items ei JOIN summaries su2 USING (summary_id)
              WHERE ei.edition_date < ?
          )
        QUALIFY row_number() OVER (
                    PARTITION BY st.story_id ORDER BY su.created_at DESC
                ) = 1
            AND row_number() OVER (
                    PARTITION BY sm.members_key
                    ORDER BY st.created_at DESC, su.created_at DESC
                ) = 1
        ORDER BY coalesce(g.tipoff, st.created_at), st.story_id
        """,
        [edition_date, *PUBLISHABLE, edition_date, edition_date, edition_date],
    ).fetchall()


def _citations(con: duckdb.DuckDBPyConnection, story_id: str) -> list[tuple[str, str, str]]:
    """(publisher, title, url) per member article, deduplicated."""
    return con.execute(
        """
        SELECT DISTINCT s.publisher, coalesce(a.title, a.url), a.url
        FROM story_articles sa
        JOIN articles a USING (article_id)
        JOIN sources s ON s.source_id = a.source_id
        WHERE sa.story_id = ?
        ORDER BY 1, 2
        """,
        [story_id],
    ).fetchall()


def _item_md(headline: str, body: str, citations: list[tuple[str, str, str]]) -> str:
    lines = [f"### {headline}", "", body, "", "Sources:", ""]
    lines += [f"- [{pub}: {title}]({url})" for pub, title, url in citations]
    return "\n".join(lines)


def render_edition(
    con: duckdb.DuckDBPyConnection,
    edition_date: date,
    out_dir: Path,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Write <out_dir>/<date>.qmd and record the edition. Returns the path,
    or None when there is nothing publishable (no empty editions)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    rows = _edition_rows(con, edition_date)
    if not rows:
        return None

    sections: dict[str, list[str]] = {s: [] for s in SECTION_ORDER}
    items: list[tuple[str, str, int]] = []  # (summary_id, section, position)
    for summary_id, story_id, kind, _game_uid, text_en, tipoff, _md, updated in rows:
        section = KIND_TO_SECTION.get(kind, "team_news")
        headline, _, body = text_en.partition("\n\n")
        # The game date tells the reader which night this was, and the
        # updated marker tells them why a story from an earlier edition is
        # back (it gained sources) instead of reading as a rehash
        # (owner's read, 2026-10-22).
        if kind == "game" and tipoff is not None:
            note = f"*Played {tipoff:%A, %d %B %Y}*"
            if updated:
                note += " *(updated with new sources)*"
            body = f"{note}\n\n{body}"
        citations = _citations(con, story_id)
        sections[section].append(_item_md(headline.strip(), body.strip(), citations))
        items.append((summary_id, section, len(sections[section])))

    parts = [
        "---",
        f'title: "BBL Daily - {edition_date:%d %B %Y}"',
        f"date: {edition_date.isoformat()}",
        "categories: [bbl]",
        "lang: en",
        "---",
        "",
    ]
    for section in SECTION_ORDER:
        if sections[section]:
            parts += [f"## {SECTION_TITLES[section]}", ""]
            parts += ["\n\n".join(sections[section]), ""]
    content = "\n".join(parts).rstrip() + "\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{edition_date.isoformat()}.qmd"
    path.write_text(content, encoding="utf-8")

    con.execute(
        """
        INSERT INTO editions VALUES (?, 'draft', ?, NULL)
        ON CONFLICT (edition_date) DO UPDATE SET built_at = excluded.built_at
        """,
        [edition_date, now],
    )
    for summary_id, section, position in items:
        con.execute(
            """
            INSERT INTO edition_items VALUES (?, ?, ?, ?)
            ON CONFLICT (edition_date, summary_id) DO UPDATE SET
                section = excluded.section, position = excluded.position
            """,
            [edition_date, summary_id, section, position],
        )
    return path
