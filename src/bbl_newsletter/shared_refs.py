"""Canonical BBL identifiers from the shared reference package.

The package is published from the NAS database by the vault's
Databases/bbl_shared_keys.py (schema v2): one directory named by its
reference_id holding a manifest and one JSONL file per shared table. It is
imported read-only; this pipeline never mints canonical IDs.

The newsletter already keys on the official ID spaces (teams.bbl_id is the
official BBL team ID, games.game_uid is '<competition>:<official game id>'),
so linking is a join, never a name match. EuroLeague and other non-BBL
competitions are outside the reference and stay unmapped by design.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

SCHEMA_VERSION = 2
# sha256 of bbl_shared_keys.SCHEMA (v2); a package built from any other
# schema is refused rather than loaded by column guesswork.
SCHEMA_SHA256 = "0ad649180cbba89106084ca8722a6b9b91e648996d6df9b89d0403174e522ece"

TABLES = {
    "shared_players": ("player_id", "bbl_real_player_id", "player_name"),
    "shared_teams": ("team_id", "team_name"),
    "shared_franchises": ("franchise_id", "bbl_team_id"),
    "shared_seasons": ("season_id", "start_year", "end_year", "label"),
    "shared_team_keys": ("season_id", "bbl_team_id", "franchise_id", "team_id"),
    "shared_games": ("game_id", "game_uid", "season_id", "competition",
                     "home_team_id", "away_team_id",
                     "home_franchise_id", "away_franchise_id"),
}
PRIMARY_KEYS = {
    "shared_players": ("player_id",),
    "shared_teams": ("team_id",),
    "shared_franchises": ("franchise_id",),
    "shared_seasons": ("season_id",),
    "shared_team_keys": ("season_id", "bbl_team_id"),
    "shared_games": ("game_id",),
}
# Descriptive context may be corrected upstream; identities may not.
MUTABLE = {
    "shared_players": {"player_name"},
    "shared_teams": {"team_name"},
    "shared_seasons": {"label"},
    "shared_games": {"home_team_id", "away_team_id",
                     "home_franchise_id", "away_franchise_id"},
}

DDL = """
CREATE TABLE IF NOT EXISTS shared_players (
  player_id INTEGER PRIMARY KEY, bbl_real_player_id INTEGER NOT NULL UNIQUE,
  player_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shared_teams (
  team_id INTEGER PRIMARY KEY, team_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shared_franchises (
  franchise_id INTEGER PRIMARY KEY, bbl_team_id INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS shared_seasons (
  season_id INTEGER PRIMARY KEY, start_year INTEGER NOT NULL UNIQUE,
  end_year INTEGER NOT NULL, label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shared_team_keys (
  season_id INTEGER NOT NULL, bbl_team_id INTEGER NOT NULL,
  franchise_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
  PRIMARY KEY (season_id, bbl_team_id));
CREATE TABLE IF NOT EXISTS shared_games (
  game_id INTEGER PRIMARY KEY, game_uid TEXT NOT NULL UNIQUE,
  season_id INTEGER NOT NULL, competition TEXT NOT NULL,
  home_team_id INTEGER, away_team_id INTEGER,
  home_franchise_id INTEGER, away_franchise_id INTEGER);
CREATE TABLE IF NOT EXISTS shared_reference_manifest (
  reference_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
  loaded_at TIMESTAMP NOT NULL DEFAULT current_timestamp);

CREATE OR REPLACE VIEW canonical_games AS
SELECT g.game_uid, s.game_id, s.season_id, s.competition,
       s.home_team_id, s.away_team_id, s.home_franchise_id, s.away_franchise_id
FROM games g JOIN shared_games s USING (game_uid);

CREATE OR REPLACE VIEW canonical_article_teams AS
SELECT a.article_id, a.team_id AS team_slug, f.franchise_id
FROM article_teams a
JOIN teams t ON t.team_id = a.team_id
JOIN shared_franchises f ON f.bbl_team_id = TRY_CAST(t.bbl_id AS INTEGER);
"""

# Competitions the reference covers; everything else is out of scope.
REFERENCE_COMPETITIONS = ("bbl", "pokal")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_package(directory: Path) -> tuple[str, dict[str, list[dict]]]:
    """Verify the manifest and every file hash; return (reference_id, rows)."""
    manifest = json.loads((directory / "manifest.json").read_bytes())
    claimed = manifest.pop("reference_id")
    if (manifest.get("schema_version") != SCHEMA_VERSION
            or _sha(_canonical(manifest)) != claimed
            or directory.name != claimed):
        raise ValueError("shared reference identity/version mismatch")
    if manifest["schema_sha256"] != SCHEMA_SHA256:
        raise ValueError("shared reference schema mismatch")
    if set(manifest["files"]) != {t + ".jsonl" for t in TABLES}:
        raise ValueError("unexpected shared reference file set")
    rows: dict[str, list[dict]] = {}
    for name, entry in manifest["files"].items():
        raw = (directory / name).read_bytes()
        if _sha(raw) != entry["sha256"] or raw.count(b"\n") != entry["rows"]:
            raise ValueError(f"shared reference checksum mismatch: {name}")
        table = name[:-len(".jsonl")]
        rows[table] = [json.loads(line) for line in raw.splitlines()]
        for row in rows[table]:
            if set(row) != set(TABLES[table]):
                raise ValueError(f"shared reference columns mismatch: {table}")
    return claimed, rows


def load(con: duckdb.DuckDBPyConnection, directory: Path) -> str:
    """Import a verified package in one transaction.

    A row whose canonical identity differs from what is already loaded is
    refused: changing an issued ID needs an explicit versioned migration.
    """
    reference_id, data = read_package(directory)
    con.execute(DDL)
    con.execute("BEGIN TRANSACTION")
    try:
        for table, columns in TABLES.items():
            pk = PRIMARY_KEYS[table]
            for row in data[table]:
                values = [row[c] for c in columns]
                old = con.execute(
                    f"SELECT {', '.join(columns)} FROM {table} WHERE "
                    + " AND ".join(f"{k} = ?" for k in pk),
                    [row[k] for k in pk],
                ).fetchone()
                if old is None:
                    con.execute(
                        f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})",
                        values,
                    )
                    continue
                changed = {c for c, a, b in zip(columns, old, values) if a != b}
                if not changed:
                    continue
                if not changed <= MUTABLE.get(table, set()):
                    raise ValueError(
                        f"canonical identity changed in {table} {dict(zip(pk, (row[k] for k in pk)))}: "
                        f"{sorted(changed)}; explicit migration required"
                    )
                con.execute(
                    f"UPDATE {table} SET "
                    + ", ".join(f"{c} = ?" for c in sorted(changed))
                    + " WHERE " + " AND ".join(f"{k} = ?" for k in pk),
                    [row[c] for c in sorted(changed)] + [row[k] for k in pk],
                )
        con.execute(
            "INSERT INTO shared_reference_manifest (reference_id, schema_version) "
            "SELECT ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM shared_reference_manifest WHERE reference_id = ?)",
            [reference_id, SCHEMA_VERSION, reference_id],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return reference_id


def check(con: duckdb.DuckDBPyConnection) -> dict:
    """Report how the newsletter's own keys resolve against the reference.

    conflicts: a game whose teams disagree with the reference's teams for
    the same game_uid. Unmapped items are reported, never guessed.
    """
    comps = list(REFERENCE_COMPETITIONS)
    unmapped_teams = [r[0] for r in con.execute(
        """SELECT t.team_id FROM teams t
           LEFT JOIN shared_franchises f ON f.bbl_team_id = TRY_CAST(t.bbl_id AS INTEGER)
           WHERE f.franchise_id IS NULL ORDER BY t.team_id""").fetchall()]
    unmapped_games = [r[0] for r in con.execute(
        """SELECT g.game_uid FROM games g JOIN seasons s USING (season_uid)
           LEFT JOIN shared_games k USING (game_uid)
           WHERE s.competition IN (SELECT unnest(?)) AND k.game_id IS NULL
           ORDER BY g.game_uid""", [comps]).fetchall()]
    conflicts = [r[0] for r in con.execute(
        """SELECT g.game_uid FROM games g
           JOIN shared_games k USING (game_uid)
           JOIN teams h ON h.team_id = g.home_team_id
           JOIN teams a ON a.team_id = g.away_team_id
           LEFT JOIN shared_franchises hf ON hf.bbl_team_id = TRY_CAST(h.bbl_id AS INTEGER)
           LEFT JOIN shared_franchises af ON af.bbl_team_id = TRY_CAST(a.bbl_id AS INTEGER)
           WHERE k.home_franchise_id IS DISTINCT FROM hf.franchise_id
              OR k.away_franchise_id IS DISTINCT FROM af.franchise_id
           ORDER BY g.game_uid""").fetchall()]
    total = con.execute(
        """SELECT count(*) FROM games g JOIN seasons s USING (season_uid)
           WHERE s.competition IN (SELECT unnest(?))""", [comps]).fetchone()[0]
    return {
        "reference_games": total,
        "mapped_games": total - len(unmapped_games),
        "unmapped_games": unmapped_games,
        "unmapped_teams": unmapped_teams,
        "conflicts": conflicts,
    }
