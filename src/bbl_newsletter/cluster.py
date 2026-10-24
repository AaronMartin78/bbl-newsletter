"""Clustering: articles -> stories, deterministically.

Three steps, no embeddings:

1. tag_articles  - match team aliases (teams table + canonical tokens) in
                   title and body -> article_teams.
2. attach_games  - an article tagged with both teams of a game whose tipoff
                   is within the attachment window of its publication
                   attaches to that game (recap after tipoff, preview
                   before) -> article_games. Several candidates: nearest
                   tipoff wins; an exact tie attaches nothing (no forced
                   links).
3. build_stories - game stories: all articles attached to one game.
                   team news: connected components over non-game articles
                   that share a team and day, with an edge when word-shingle
                   Jaccard similarity marks them as the same story
                   (syndicated copies merge, distinct stories stay apart).
                   A component whose members all read as European-cup
                   coverage becomes kind='euroleague'.

Everything is idempotent: tags and memberships are keyed inserts, story ids
are deterministic functions of date + member article ids.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import duckdb

from . import ids
from .schedule import SLUG_OVERRIDES

GAME_ATTACH_WINDOW_H = 36       # preview: publication before tipoff
RECAP_ATTACH_WINDOW_H = 96      # recap: publication after tipoff; official
                                # newscenter recaps can trail by two days
SHINGLE_K = 5                   # word 5-grams
SYNDICATION_THRESHOLD = 0.5     # Jaccard above this = same story
BODY_COMPARE_WORDS = 800
EUROPEAN_CUP = re.compile(
    r"\b(euroleague|eurocup|bcl|basketball champions league|enbl|fiba europe cup)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- tagging
# Tokens safe for resolving OFFICIAL team names but far too generic for
# tagging free text: "Bayern" is the Bundesland in every BR24 politics
# article (observed live 2026-10-14: AfD and Makkabi stories tagged as the
# team). Tagging uses club-specific tokens only.
GENERIC_TOKENS = {"bayern", "frankfurt", "hamburg", "jena", "ulm",
                  "bamberg", "bonn", "trier", "hagen", "rostock",
                  "chemnitz", "oldenburg", "braunschweig", "vechta",
                  "wuerzburg", "würzburg", "weißenfels",   # umlaut forms: the
                  # schedule tokens carry umlauts, the exclusion must too
                  # (observed live 2026-10-14: Landkreis Würzburg tagged)
                  "ludwigsburg", "mbc"}


def _team_patterns(con: duckdb.DuckDBPyConnection) -> dict[str, re.Pattern]:
    """One compiled alternation per team: stored name + aliases + the
    club-specific canonical tokens (city/state names excluded)."""
    tokens: dict[str, set[str]] = {}
    for team_id, name, aliases in con.execute(
        "SELECT team_id, name, aliases FROM teams"
    ).fetchall():
        bag = tokens.setdefault(team_id, set())
        bag.add(name)
        bag.update(aliases or [])
        bag.update(t for t, slug in SLUG_OVERRIDES
                   if slug == team_id and t not in GENERIC_TOKENS)
    return {
        team_id: re.compile(
            # closing \b so a short alias cannot match inside a longer word
            # ("alba" tagged an Albanien politics story, observed 2026-10-17);
            # optional genitive-s keeps "Albas Sieg" matching
            r"\b(" + "|".join(re.escape(t) for t in sorted(bag, key=len, reverse=True)) + r")s?\b",
            re.IGNORECASE,
        )
        for team_id, bag in tokens.items() if bag
    }


def _article_text(title: Optional[str], body_path: Optional[str]) -> str:
    body = ""
    if body_path and Path(body_path).exists():
        body = Path(body_path).read_text(encoding="utf-8")
    return f"{title or ''}\n{body}"


def tag_articles(con: duckdb.DuckDBPyConnection, since: datetime) -> int:
    patterns = _team_patterns(con)
    tagged = 0
    rows = con.execute(
        """
        SELECT DISTINCT article_id, title, body_path FROM articles
        WHERE coalesce(published_at, observed_at) >= ?
          AND NOT stale
          AND article_id NOT IN (SELECT article_id FROM article_exclusions)
        """,
        [since],
    ).fetchall()
    for article_id, title, body_path in rows:
        text = _article_text(title, body_path)
        for team_id, pattern in patterns.items():
            if pattern.search(text):
                con.execute(
                    "INSERT INTO article_teams VALUES (?, ?) "
                    "ON CONFLICT DO NOTHING",
                    [article_id, team_id],
                )
                tagged += 1
    return tagged


# ---------------------------------------------------------- game attachment
def attach_games(con: duckdb.DuckDBPyConnection, since: datetime) -> int:
    window = timedelta(hours=GAME_ATTACH_WINDOW_H)
    recap_window = timedelta(hours=RECAP_ATTACH_WINDOW_H)
    games = con.execute(
        """
        SELECT game_uid, tipoff, home_team_id, away_team_id FROM games
        WHERE tipoff BETWEEN ? AND ?
        """,
        [since - recap_window, datetime.max - recap_window],
    ).fetchall()
    articles = con.execute(
        """
        SELECT a.article_id,
               min(coalesce(a.published_at, a.observed_at)) AS published,
               list(DISTINCT t.team_id) AS teams
        FROM articles a JOIN article_teams t USING (article_id)
        WHERE coalesce(a.published_at, a.observed_at) >= ?
          AND NOT a.stale
          AND a.article_id NOT IN (SELECT article_id FROM article_exclusions)
        GROUP BY a.article_id HAVING count(DISTINCT t.team_id) >= 2
        """,
        [since],
    ).fetchall()
    attached = 0
    for article_id, published, teams in articles:
        team_set = set(teams)
        candidates = [
            (abs(published - tipoff), game_uid, tipoff)
            for game_uid, tipoff, home, away in games
            if {home, away} <= team_set
            and -window <= published - tipoff <= recap_window
        ]
        if not candidates:
            continue
        candidates.sort()
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            continue  # ambiguous: attach nothing
        _, game_uid, tipoff = candidates[0]
        reference = "recap" if published >= tipoff else "preview"
        con.execute(
            "INSERT INTO article_games VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [article_id, game_uid, reference],
        )
        attached += 1
    return attached


# ------------------------------------------------------------- similarity
def _shingles(text: str, k: int = SHINGLE_K) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())[:BODY_COMPARE_WORDS]
    return {tuple(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic root: lexicographically smaller id wins
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


# ----------------------------------------------------------------- stories
def _insert_story(
    con: duckdb.DuckDBPyConnection,
    edition_date: date,
    kind: str,
    members: list[str],
    game_uid: Optional[str],
    now: datetime,
) -> str:
    # One stable story per game: its id carries no date, so articles
    # arriving on later days join it (membership inserts are keyed) and the
    # changed input hash triggers a fresh composite summary.
    if game_uid is not None:
        story = ids.game_story_id(game_uid)
    else:
        story = ids.story_id(edition_date.isoformat(), members)
    con.execute(
        "INSERT INTO stories VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
        [story, kind, game_uid, now],
    )
    for article_id in members:
        con.execute(
            "INSERT INTO story_articles VALUES (?, ?) ON CONFLICT DO NOTHING",
            [story, article_id],
        )
    return story


def build_stories(
    con: duckdb.DuckDBPyConnection,
    edition_date: date,
    window_hours: int = 36,
    now: Optional[datetime] = None,
) -> list[str]:
    now = now or datetime.combine(edition_date, datetime.min.time())
    since = datetime.combine(edition_date, datetime.min.time()) - timedelta(
        hours=window_hours
    )
    stories: list[str] = []

    # Game stories: one per recent game; members are ALL attached articles,
    # so a days-old preview still contributes its angle to the composite.
    # The lookback spans the recap window, so a game whose recap arrived
    # days after tipoff still gets its membership (and summary) refreshed.
    game_rows = con.execute(
        """
        SELECT g.game_uid, list(DISTINCT ag.article_id ORDER BY ag.article_id)
        FROM games g JOIN article_games ag USING (game_uid)
        WHERE g.tipoff >= ?
        GROUP BY g.game_uid
        """,
        [since - timedelta(hours=RECAP_ATTACH_WINDOW_H)],
    ).fetchall()
    game_attached: set[str] = set()
    for game_uid, members in game_rows:
        stories.append(_insert_story(con, edition_date, "game", members, game_uid, now))
        game_attached.update(members)

    # Team news: non-game articles sharing team + day, merged by similarity.
    rows = con.execute(
        """
        SELECT a.article_id, t.team_id,
               CAST(coalesce(a.published_at, a.observed_at) AS DATE),
               a.title, a.body_path
        FROM articles a JOIN article_teams t USING (article_id)
        WHERE coalesce(a.published_at, a.observed_at) >= ?
          AND NOT a.stale
          AND a.article_id NOT IN (SELECT article_id FROM article_exclusions)
        """,
        [since],
    ).fetchall()
    rows = [r for r in rows if r[0] not in game_attached]
    if not rows:
        return stories

    texts: dict[str, str] = {}
    groups: dict[tuple[str, date], list[str]] = {}
    for article_id, team_id, day, title, body_path in rows:
        if article_id not in texts:
            texts[article_id] = _article_text(title, body_path)
        groups.setdefault((team_id, day), []).append(article_id)

    shingle_cache = {aid: _shingles(text) for aid, text in texts.items()}
    uf = _UnionFind(texts.keys())
    for members in groups.values():
        members = sorted(set(members))
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if jaccard(shingle_cache[a], shingle_cache[b]) >= SYNDICATION_THRESHOLD:
                    uf.union(a, b)

    components: dict[str, list[str]] = {}
    for aid in texts:
        components.setdefault(uf.find(aid), []).append(aid)
    for members in components.values():
        members = sorted(members)
        kind = (
            "euroleague"
            if all(EUROPEAN_CUP.search(texts[m]) for m in members)
            else "team_news"
        )
        stories.append(_insert_story(con, edition_date, kind, members, None, now))
    return stories


def cluster(
    con: duckdb.DuckDBPyConnection,
    edition_date: date,
    window_hours: int = 36,
    now: Optional[datetime] = None,
) -> dict:
    """Run the full clustering stage for one edition date.

    Tagging and game attachment look further back than the edition window
    (window_hours + GAME_ATTACH_WINDOW_H), so previews published well before
    a recent game are tagged and attached; story membership itself is driven
    by game tipoff (game stories) or publication (team news) within
    window_hours of the edition.
    """
    article_since = datetime.combine(edition_date, datetime.min.time()) - timedelta(
        hours=window_hours + GAME_ATTACH_WINDOW_H
    )
    tags = tag_articles(con, article_since)
    attachments = attach_games(con, article_since)
    stories = build_stories(con, edition_date, window_hours, now=now)
    return {"tags": tags, "game_attachments": attachments, "stories": stories}
