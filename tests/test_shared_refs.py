import hashlib
import json
from pathlib import Path

import pytest

from bbl_newsletter import db, shared_refs

from helpers import TIPOFF, seed_base

SCHEMA = Path(__file__).parents[1] / "schema.sql"

# Mirrors a package published by the vault's bbl_shared_keys.py: franchise_id
# equals the official team ID, game_uid is '<competition>:<official game id>'.
REFERENCE = {
    "shared_players": [{"player_id": 1, "bbl_real_player_id": 5001, "player_name": "Carsen Edwards"}],
    "shared_teams": [{"team_id": 7, "team_name": "FC Bayern München"},
                     {"team_id": 8, "team_name": "Science City Jena"}],
    "shared_franchises": [{"franchise_id": 10, "bbl_team_id": 10},
                          {"franchise_id": 30, "bbl_team_id": 30}],
    "shared_seasons": [{"season_id": 3, "start_year": 2026, "end_year": 2027, "label": "2026-27"}],
    "shared_team_keys": [{"season_id": 3, "bbl_team_id": 10, "franchise_id": 10, "team_id": 7},
                         {"season_id": 3, "bbl_team_id": 30, "franchise_id": 30, "team_id": 8}],
    "shared_games": [{"game_id": 1, "game_uid": "bbl:1", "season_id": 3, "competition": "BBL",
                      "home_team_id": 7, "away_team_id": 8,
                      "home_franchise_id": 10, "away_franchise_id": 30}],
}


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def publish(root: Path, tables: dict) -> Path:
    files = {f"{t}.jsonl": b"".join(_canonical(r) + b"\n" for r in rows)
             for t, rows in tables.items()}
    info = {
        "schema_version": shared_refs.SCHEMA_VERSION,
        "schema_sha256": shared_refs.SCHEMA_SHA256,
        "files": {n: {"sha256": hashlib.sha256(raw).hexdigest(), "rows": raw.count(b"\n")}
                  for n, raw in files.items()},
    }
    refid = hashlib.sha256(_canonical(info)).hexdigest()
    directory = root / refid
    directory.mkdir(parents=True)
    for name, raw in files.items():
        (directory / name).write_bytes(raw)
    (directory / "manifest.json").write_bytes(_canonical({"reference_id": refid, **info}) + b"\n")
    return directory


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_schema(c, SCHEMA)
    seed_base(c)
    yield c
    c.close()


def test_link_maps_newsletter_keys_to_canonical_ids(con, tmp_path):
    ref = publish(tmp_path / "ref", REFERENCE)
    assert shared_refs.load(con, ref) == ref.name
    report = shared_refs.check(con)
    assert report == {"reference_games": 1, "mapped_games": 1, "unmapped_games": [],
                      "unmapped_teams": ["hamburg"], "conflicts": []}
    assert con.execute(
        "SELECT game_id, home_team_id, away_team_id FROM canonical_games WHERE game_uid = 'bbl:1'"
    ).fetchone() == (1, 7, 8)


def test_out_of_scope_competitions_are_not_counted(con, tmp_path):
    con.execute("INSERT INTO seasons VALUES ('euroleague-2026-27', 'euroleague', '2026-27', NULL, NULL)")
    con.execute("INSERT INTO games VALUES ('euroleague:9', 'euroleague-2026-27', 3, ?, 'bayern', 'hamburg', NULL, NULL, 'scheduled')",
                [TIPOFF])
    shared_refs.load(con, publish(tmp_path / "ref", REFERENCE))
    report = shared_refs.check(con)
    assert report["reference_games"] == 1
    assert "euroleague:9" not in report["unmapped_games"]


def test_missing_reference_game_is_reported_not_guessed(con, tmp_path):
    con.execute("INSERT INTO games VALUES ('bbl:2', 'bbl-2026-27', 6, ?, 'jena', 'bayern', NULL, NULL, 'scheduled')",
                [TIPOFF])
    shared_refs.load(con, publish(tmp_path / "ref", REFERENCE))
    report = shared_refs.check(con)
    assert report["unmapped_games"] == ["bbl:2"]
    assert report["mapped_games"] == 1


def test_team_disagreement_is_a_conflict(con, tmp_path):
    tables = dict(REFERENCE)
    tables["shared_games"] = [dict(REFERENCE["shared_games"][0], home_franchise_id=30, away_franchise_id=10)]
    shared_refs.load(con, publish(tmp_path / "ref", tables))
    assert shared_refs.check(con)["conflicts"] == ["bbl:1"]


def test_reload_is_idempotent_and_names_may_change(con, tmp_path):
    shared_refs.load(con, publish(tmp_path / "a", REFERENCE))
    tables = dict(REFERENCE)
    tables["shared_teams"] = [{"team_id": 7, "team_name": "FC Bayern Basketball"}, REFERENCE["shared_teams"][1]]
    shared_refs.load(con, publish(tmp_path / "b", tables))
    assert con.execute("SELECT team_name FROM shared_teams WHERE team_id = 7").fetchone() == ("FC Bayern Basketball",)
    assert con.execute("SELECT count(*) FROM shared_reference_manifest").fetchone() == (2,)


def test_changed_canonical_identity_is_refused(con, tmp_path):
    shared_refs.load(con, publish(tmp_path / "a", REFERENCE))
    tables = dict(REFERENCE)
    tables["shared_players"] = [dict(REFERENCE["shared_players"][0], bbl_real_player_id=9999)]
    with pytest.raises(ValueError, match="explicit migration required"):
        shared_refs.load(con, publish(tmp_path / "b", tables))
    assert con.execute("SELECT bbl_real_player_id FROM shared_players").fetchone() == (5001,)


def test_tampered_package_is_refused(con, tmp_path):
    ref = publish(tmp_path / "ref", REFERENCE)
    (ref / "shared_games.jsonl").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        shared_refs.load(con, ref)
