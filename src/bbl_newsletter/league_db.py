"""Teams and games for a league whose daily collector already builds a database.

Used when the league config says `games_source: league_db` (ACB, LNB, GBL,
ABA). The league SQLite on the NAS lake is opened read-only; the newsletter
never scrapes a schedule for these leagues and never mints ids. Keys:

- game_uid = '<prefix>:<games.game_id>', the league database's own game id,
  so stories join straight to box scores and statistics;
- team_id  = '<league>:<franchise_id>', the provider's stable club id, with
  that id also in teams.bbl_id (the column holds the league's provider club
  id for these leagues).

Only the latest season in the database is loaded.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .league import League

STATUS_MAP = {"OFFICIAL": "final", "FORFEIT": "final",
              "CANCELLED": "cancelled", "POSTPONED": "postponed"}


def load_games(con: duckdb.DuckDBPyConnection, league: League, source: Path) -> dict:
    """Upsert the latest season's teams and games; returns counts."""
    prefix_of = {code: prefix for prefix, code in league.competitions.items()}
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        season = db.execute(
            "SELECT season_id, label FROM seasons ORDER BY start_year DESC LIMIT 1").fetchone()
        games = db.execute(
            "SELECT * FROM games WHERE season_id = ? ORDER BY game_id", [season["season_id"]]).fetchall()
        aliases: dict[int, set[str]] = {}
        for row in db.execute("SELECT a.team_id, a.alias, t.team_name FROM team_aliases a "
                              "JOIN teams t USING (team_id)"):
            aliases.setdefault(row["team_id"], {row["team_name"]}).add(row["alias"])
    finally:
        db.close()

    teams = {}
    for g in games:
        for side, db_side in (("home", "home"), ("away", "guest")):
            franchise = g[f"{db_side}_franchise_id"]
            teams[franchise] = (g[f"{side}_team_id"], g[f"{db_side}_team_name"])
    for franchise, (db_team_id, name) in teams.items():
        names = sorted(aliases.get(db_team_id, {name}) | set(league.aliases.get(str(franchise), [])))
        team_id = f"{league.code}:{franchise}"
        con.execute("INSERT INTO teams (team_id, bbl_id, name, city, aliases) VALUES (?, ?, ?, NULL, []) "
                    "ON CONFLICT DO NOTHING", [team_id, str(franchise), name])
        for alias in names:
            con.execute("INSERT INTO team_aliases VALUES (?, ?) ON CONFLICT DO NOTHING", [team_id, alias])

    loaded = 0
    for g in games:
        prefix = prefix_of.get(g["competition"])
        if prefix is None or not g["scheduled_time"]:
            continue
        season_uid = f"{prefix}-{season['label']}"
        con.execute(
            "INSERT INTO seasons (season_uid, competition, label) SELECT ?, ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM seasons WHERE season_uid = ?)",
            [season_uid, prefix, season["label"], season_uid],
        )
        status = STATUS_MAP.get(g["status"], "scheduled")
        final = status == "final"
        tipoff = datetime.fromisoformat(g["scheduled_time"].replace("Z", "+00:00"))
        con.execute(
            """
            INSERT INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (game_uid) DO UPDATE SET
                status = excluded.status, tipoff = excluded.tipoff, matchday = excluded.matchday,
                home_score = excluded.home_score, away_score = excluded.away_score
            """,
            [f"{prefix}:{g['game_id']}", season_uid, g["match_day"],
             tipoff.astimezone(timezone.utc).replace(tzinfo=None),
             f"{league.code}:{g['home_franchise_id']}", f"{league.code}:{g['guest_franchise_id']}",
             g["home_final_score"] if final else None, g["guest_final_score"] if final else None,
             status],
        )
        loaded += 1
    return {"season": season["label"], "teams": len(teams), "games": loaded}
