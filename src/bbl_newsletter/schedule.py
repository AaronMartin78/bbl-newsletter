"""Schedule loader: BBL game pages -> seasons, teams, games tables.

Field contract of easycredit-bbl.de game pages (__NEXT_DATA__ ->
props.pageProps.initialGameData), taken from the reviewed, working Hermes
public collector (bbl_public.py / bbl_current.py / bbl_merge.py), not
guessed: id, seasonId, competition ("BBL" | "BBL_CUP"), scheduledTime
(ISO, Z), status ("OFFICIAL" when final), matchDay, homeTeam/guestTeam
{teamId, seasonTeamId, name}, result {homeTeamFinalScore,
guestTeamFinalScore}. Verify against a live page on the first LAN run.

Game enumeration (contract observed live 2026-10-13 on /spiele/2006881):
each game page also embeds initialRecentGames with past/present/future
lists whose entries are COMPLETE game objects of the same shape as
initialGameData. One page therefore yields up to ~27 game records. The
loader seeds game ids from a listing page's /spiele/<id> links, fetches a
bounded number of pages, upserts every embedded game, and walks to
non-final neighbors until the window is covered or the page budget is
spent. Finished games are never re-fetched as pages.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional

import duckdb

from .fetch import Fetcher
from .ids import game_uid

GAME_LINK = re.compile(r"/spiele/(\d+)")

# Public games API behind the website, same mechanism the site itself uses
# (contract from the reviewed Hermes public collector, bbl_public.py): the
# credential is embedded in every page's __NEXT_DATA__ as pageProps.key and
# sent back reversed. One season query returns EVERY game, league and cup -
# the page-walk below never reached cup games (observed 2026-10-19: zero
# Pokal rows, so no recap could attach and every game stayed a single-source
# story). The walk remains as score enrichment and fallback.
API_BASE = "https://api.basketball-bundesliga.de"
API_PAGE_SIZE = 100
MAX_API_PAGES = 20

COMPETITION_MAP = {"BBL": "bbl", "BBL_CUP": "pokal"}
STATUS_MAP = {"OFFICIAL": "final", "CANCELLED": "cancelled", "POSTPONED": "postponed"}

# Official team names -> our canonical registry slugs (registry `covers` and
# relevance keywords use these). Substring match, case-insensitive, reviewed
# against the 18 clubs of 2026-27. Unmatched teams get a slugified name.
SLUG_OVERRIDES = [
    ("bayern", "bayern"), ("alba", "alba"), ("ulm", "ulm"),
    ("bamberg", "bamberg"), ("würzburg", "wuerzburg"), ("wuerzburg", "wuerzburg"),
    ("chemnitz", "chemnitz"), ("niners", "chemnitz"), ("jena", "jena"),
    ("ludwigsburg", "ludwigsburg"), ("skyliners", "frankfurt"),
    ("frankfurt", "frankfurt"), ("hamburg", "hamburg"), ("towers", "hamburg"),
    ("oldenburg", "oldenburg"), ("braunschweig", "braunschweig"),
    ("vechta", "vechta"), ("bonn", "bonn"), ("trier", "trier"),
    ("hagen", "hagen"), ("rostock", "rostock"), ("seawolves", "rostock"),
    ("mitteldeutscher", "mbc"), ("mbc", "mbc"), ("weißenfels", "mbc"),
]


@dataclass
class GameRecord:
    game_id: str
    season_id: int
    competition: str  # mapped: bbl | pokal
    status: str       # mapped: scheduled | final | postponed | cancelled
    scheduled_time: datetime  # naive UTC
    matchday: Optional[int]
    home_bbl_id: str
    home_name: str
    guest_bbl_id: str
    guest_name: str
    home_score: Optional[int]
    guest_score: Optional[int]

    @property
    def uid(self) -> str:
        return game_uid(self.competition, self.game_id)

    @property
    def season_uid(self) -> str:
        return f"{self.competition}-{self.season_id}-{(self.season_id + 1) % 100:02d}"


class _NextData(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self._active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._active = False

    def handle_data(self, data):
        if self._active:
            self.parts.append(data)


def page_props(html: str | bytes) -> dict:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    parser = _NextData()
    parser.feed(html)
    if not parser.parts:
        raise ValueError("no __NEXT_DATA__ script found")
    props = json.loads("".join(parser.parts))["props"]["pageProps"]
    if not isinstance(props, dict):
        raise ValueError("invalid page props")
    return props


def parse_game_page(html: str | bytes, expected_id: Optional[str] = None) -> GameRecord:
    game = page_props(html).get("initialGameData")
    if not isinstance(game, dict):
        raise ValueError("initialGameData missing")
    return record_from_game(game, expected_id=expected_id)


def games_from_page(html: str | bytes) -> list[dict]:
    """All complete game objects a game page carries: its own game plus the
    embedded initialRecentGames past/present/future entries."""
    props = page_props(html)
    games: list[dict] = []
    main = props.get("initialGameData")
    if isinstance(main, dict):
        games.append(main)
    recent = props.get("initialRecentGames") or {}
    for key in ("past", "present", "future"):
        games.extend(g for g in (recent.get(key) or []) if isinstance(g, dict))
    return games


def record_from_game(game: dict, expected_id: Optional[str] = None) -> GameRecord:
    gid = str(game["id"])
    if expected_id is not None and gid != str(expected_id):
        raise ValueError(f"game id mismatch: page {gid}, expected {expected_id}")
    competition = COMPETITION_MAP.get(game["competition"])
    if competition is None:
        raise ValueError(f"unexpected competition: {game['competition']}")
    # TBD games (next cup round placeholders, isTBD true) carry a null
    # scheduledTime; they cannot anchor the recap attachment window, so skip
    # them until the API assigns a time (observed live 2026-10-20, where the
    # None killed the whole schedule stage via an uncaught AttributeError).
    raw_time = game.get("scheduledTime")
    if not raw_time:
        raise ValueError("no scheduledTime (TBD game)")
    scheduled = datetime.fromisoformat(
        raw_time.replace("Z", "+00:00")
    ).astimezone(timezone.utc).replace(tzinfo=None)
    result = game.get("result") or {}
    final = game["status"] == "OFFICIAL"

    # The games API marks a game OFFICIAL while its final scores are still
    # null (observed live 2026-10-19 on the Pokal round); never let that
    # raise, and never let it overwrite a known score (upsert coalesces).
    def score(field: str) -> Optional[int]:
        value = result.get(field)
        return int(value) if final and value is not None else None

    return GameRecord(
        game_id=gid,
        season_id=int(game["seasonId"]),
        competition=competition,
        status=STATUS_MAP.get(game["status"], "scheduled"),
        scheduled_time=scheduled,
        matchday=game.get("matchDay"),
        home_bbl_id=str(game["homeTeam"]["teamId"]),
        home_name=game["homeTeam"]["name"],
        guest_bbl_id=str(game["guestTeam"]["teamId"]),
        guest_name=game["guestTeam"]["name"],
        home_score=score("homeTeamFinalScore"),
        guest_score=score("guestTeamFinalScore"),
    )


def game_ids_from_listing(html: str | bytes) -> list[str]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    seen: list[str] = []
    for gid in GAME_LINK.findall(html):
        if gid not in seen:
            seen.append(gid)
    return seen


def _slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def resolve_team(con: duckdb.DuckDBPyConnection, bbl_id: str, name: str) -> str:
    """Return our team slug for an official team id, creating the row once."""
    row = con.execute(
        "SELECT team_id FROM teams WHERE bbl_id = ?", [bbl_id]
    ).fetchone()
    if row:
        return row[0]
    lowered = name.lower()
    slug = next((s for token, s in SLUG_OVERRIDES if token in lowered), None) or _slugify(name)
    existing = con.execute(
        "SELECT team_id, bbl_id FROM teams WHERE team_id = ?", [slug]
    ).fetchone()
    if existing:
        if existing[1] is None:  # seeded row without official id: adopt it
            con.execute(
                "UPDATE teams SET bbl_id = ?, aliases = list_append(aliases, ?) "
                "WHERE team_id = ?",
                [bbl_id, name, slug],
            )
        return slug
    con.execute(
        "INSERT INTO teams (team_id, bbl_id, name, city, aliases) VALUES (?, ?, ?, NULL, ?)",
        [slug, bbl_id, name, [name]],
    )
    return slug


def _ensure_season(con: duckdb.DuckDBPyConnection, rec: GameRecord) -> None:
    con.execute(
        """
        INSERT INTO seasons (season_uid, competition, label)
        SELECT ?, ?, ? WHERE NOT EXISTS
            (SELECT 1 FROM seasons WHERE season_uid = ?)
        """,
        [rec.season_uid, rec.competition,
         f"{rec.season_id}-{(rec.season_id + 1) % 100:02d}", rec.season_uid],
    )


def upsert_game(con: duckdb.DuckDBPyConnection, rec: GameRecord) -> None:
    _ensure_season(con, rec)
    home = resolve_team(con, rec.home_bbl_id, rec.home_name)
    guest = resolve_team(con, rec.guest_bbl_id, rec.guest_name)
    con.execute(
        """
        INSERT INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_uid) DO UPDATE SET
            status = excluded.status,
            tipoff = excluded.tipoff,
            matchday = excluded.matchday,
            home_score = coalesce(excluded.home_score, games.home_score),
            away_score = coalesce(excluded.away_score, games.away_score)
        """,
        [rec.uid, rec.season_uid, rec.matchday, rec.scheduled_time,
         home, guest, rec.home_score, rec.guest_score, rec.status],
    )


def _is_final(con: duckdb.DuckDBPyConnection, gid: str) -> bool:
    row = con.execute(
        "SELECT status FROM games WHERE game_uid IN (?, ?)",
        [game_uid("bbl", gid), game_uid("pokal", gid)],
    ).fetchone()
    return bool(row and row[0] in ("final", "cancelled"))


def current_season_id(now: Optional[datetime] = None) -> int:
    """BBL seasons are identified by their starting year; the season rolls
    over during the summer break (July)."""
    now = now or datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def collect_schedule_api(
    con: duckdb.DuckDBPyConnection,
    fetcher: Fetcher,
    listing_url: str,
    season_id: Optional[int] = None,
) -> dict:
    """Upsert the whole season (league + cup) from the public games API.
    The listing page is fetched only for the embedded API key."""
    season_id = season_id or current_season_id()
    report = {"season_id": season_id, "pages_fetched": 0, "upserted": 0,
              "errors": []}
    listing = fetcher.get(listing_url)
    if listing.status != 200 or listing.content is None:
        report["errors"].append(f"{listing_url}: {listing.error or listing.status}")
        return report
    try:
        key = page_props(listing.content).get("key")
    except ValueError as exc:
        report["errors"].append(f"{listing_url}: {exc}")
        return report
    if not isinstance(key, str) or not key:
        report["errors"].append("no API key in listing page props")
        return report
    headers = {
        "Accept": "application/json",
        "x-api-key": "publicWebUser",
        "x-api-secret": key[::-1],
    }
    page_number, total_pages = 1, 1
    while page_number <= min(total_pages, MAX_API_PAGES):
        page = fetcher.get(
            f"{API_BASE}/games?currentPage={page_number}"
            f"&pageSize={API_PAGE_SIZE}&seasonId={season_id}",
            headers=headers,
        )
        report["pages_fetched"] += 1
        if page.status != 200 or page.content is None:
            report["errors"].append(f"api page {page_number}: {page.error or page.status}")
            return report
        try:
            data = json.loads(page.content)
            total_pages = int(data.get("totalPages", 1))
            items = data.get("items") or []
        except (ValueError, TypeError) as exc:
            report["errors"].append(f"api page {page_number}: parse: {exc}")
            return report
        for game in items:
            try:
                rec = record_from_game(game)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                report["errors"].append(f"api game {game.get('id')}: {exc}")
                continue
            upsert_game(con, rec)
            report["upserted"] += 1
        page_number += 1
    return report


def collect_schedule(
    con: duckdb.DuckDBPyConnection,
    fetcher: Fetcher,
    listing_url: str,
    base_url: str = "https://www.easycredit-bbl.de",
    max_pages: int = 6,
) -> dict:
    """Seed game ids from the listing, fetch a bounded number of game pages,
    upsert every embedded game record, and walk to non-final neighbors."""
    report = {"listing": listing_url, "ids_seen": 0, "pages_fetched": 0,
              "upserted": 0, "errors": []}
    listing = fetcher.get(listing_url)
    if listing.status != 200 or listing.content is None:
        report["errors"].append(f"{listing_url}: {listing.error or listing.status}")
        return report
    frontier = game_ids_from_listing(listing.content)
    report["ids_seen"] = len(frontier)
    # known upcoming games are seeds too, so the walk continues even when
    # the listing exposes only an already-finished game
    frontier += [
        uid.split(":", 1)[1] for (uid,) in con.execute(
            "SELECT game_uid FROM games WHERE status NOT IN ('final', 'cancelled')"
        ).fetchall()
    ]
    visited: set[str] = set()
    while frontier and report["pages_fetched"] < max_pages:
        gid = frontier.pop(0)
        if gid in visited:
            continue
        visited.add(gid)
        # a final game is not worth a page fetch, except as the run's first
        # page: that anchor fetch is what reveals the embedded neighbors
        if _is_final(con, gid) and report["pages_fetched"] > 0:
            continue
        page = fetcher.get(f"{base_url}/spiele/{gid}")
        report["pages_fetched"] += 1
        if page.status != 200 or page.content is None:
            report["errors"].append(f"{gid}: {page.error or page.status}")
            continue
        try:
            games = games_from_page(page.content)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            report["errors"].append(f"{gid}: parse: {exc}")
            continue
        for game in games:
            try:
                rec = record_from_game(game)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                report["errors"].append(f"embedded in {gid}: {exc}")
                continue
            upsert_game(con, rec)
            report["upserted"] += 1
            if rec.status not in ("final", "cancelled") and rec.game_id not in visited:
                frontier.append(rec.game_id)
    return report
