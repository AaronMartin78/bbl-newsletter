"""League profiles: a league_db league (ACB here) loads its teams and games from
the league database, gets its box scores from the same file, and is written
up in its own language settings; the BBL default is unchanged."""
import dataclasses
import sqlite3
from pathlib import Path

import duckdb
import pytest

from bbl_newsletter import db
from bbl_newsletter.boxscores import STATS, load_boxscores
from bbl_newsletter.league import BBL, League
from bbl_newsletter.league_db import load_games
from bbl_newsletter.registry import Source
from bbl_newsletter.relevance import is_candidate, is_promo
from bbl_newsletter.summarize import build_prompt, validate

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"
ACB = League.from_config({"league": {
    "code": "acb", "name": "Liga Endesa", "description": "Spanish professional basketball (Liga Endesa)",
    "source_language": "es", "competitions": {"acb": "ACB"}, "games_source": "league_db",
    "official_game_url": "https://live.acb.com/es/partidos/{game_id}/estadisticas",
    "player_identity_column": "acb_player_id", "edition_title": "Liga Endesa Daily",
    "aliases": {"14": ["Baskonia"]}, "keywords": ["baloncesto", "acb"],
}})


@pytest.fixture
def league_db(tmp_path):
    """A tiny league database in the shape the ACB collector writes."""
    path = tmp_path / "acb.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE seasons (season_id INTEGER PRIMARY KEY, start_year INTEGER, label TEXT);
        CREATE TABLE teams (team_id INTEGER PRIMARY KEY, team_name TEXT);
        CREATE TABLE team_aliases (alias TEXT, team_id INTEGER);
        CREATE TABLE games (game_id INTEGER PRIMARY KEY, season_id INTEGER, competition TEXT,
            status TEXT, scheduled_time TEXT, match_day INTEGER,
            home_team_id INTEGER, away_team_id INTEGER, home_franchise_id INTEGER,
            guest_franchise_id INTEGER, home_team_name TEXT, guest_team_name TEXT,
            home_final_score INTEGER, guest_final_score INTEGER, overtime_periods INTEGER,
            home_q1_score INTEGER, home_q2_score INTEGER, home_q3_score INTEGER, home_q4_score INTEGER,
            guest_q1_score INTEGER, guest_q2_score INTEGER, guest_q3_score INTEGER, guest_q4_score INTEGER,
            home_overtime_score INTEGER, guest_overtime_score INTEGER);
        INSERT INTO seasons VALUES (1, 2025, '2025-26'), (2, 2026, '2026-27');
        INSERT INTO teams VALUES (1, 'Kosner Baskonia'), (2, 'Unicaja');
        INSERT INTO team_aliases VALUES ('Kosner Baskonia', 1), ('Baskonia Vitoria-Gasteiz', 1), ('Unicaja', 2);
        INSERT INTO games VALUES (900, 1, 'ACB', 'OFFICIAL', '2026-05-01T18:00:00Z', 30, 1, 2, 14, 4,
            'Kosner Baskonia', 'Unicaja', 70, 60, 0, 20,20,20,10, 15,15,15,15, NULL, NULL);
        INSERT INTO games VALUES (105410, 2, 'ACB', 'OFFICIAL', '2026-11-08T17:00:00Z', 7, 1, 2, 14, 4,
            'Kosner Baskonia', 'Unicaja', 89, 76, 0, 20,20,20,29, 19,19,19,19, NULL, NULL);
        INSERT INTO games VALUES (105420, 2, 'ACB', 'PRE', '2026-11-15T17:00:00Z', 8, 2, 1, 4, 14,
            'Unicaja', 'Kosner Baskonia', NULL, NULL, 0, NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL, NULL, NULL);
    """)
    c.execute("CREATE TABLE game_team_stats (game_id INTEGER, team_id INTEGER, franchise_id INTEGER, "
              "home_away TEXT," + ",".join(k + " REAL" for k in STATS) + ")")
    c.execute("CREATE TABLE game_player_stats (game_id INTEGER, team_id INTEGER, franchise_id INTEGER, "
              "player_id INTEGER, acb_player_id INTEGER, player_name TEXT,"
              + ",".join(k + " REAL" for k in STATS) + ")")
    for tid, fid, side, points in [(1, 14, "home", 89), (2, 4, "away", 76)]:
        stats = [None] * len(STATS)
        stats[STATS.index("points")] = points
        vals = [105410, tid, fid, side, *stats]
        c.execute("INSERT INTO game_team_stats VALUES (" + ",".join("?" * len(vals)) + ")", vals)
        vals = [105410, tid, fid, tid + 10, 20200835 + tid, "Player", *stats]
        c.execute("INSERT INTO game_player_stats VALUES (" + ",".join("?" * len(vals)) + ")", vals)
    c.commit()
    c.close()
    return path


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    db.init_schema(c, SCHEMA)
    yield c
    c.close()


def test_bbl_is_the_default_profile():
    assert League.from_config({}) == BBL
    assert BBL.posts_dir == "bbl/posts"


def test_unknown_language_is_refused():
    with pytest.raises(SystemExit):
        League.from_config({"league": {"source_language": "xx"}})


def test_latest_season_loads_with_league_ids(con, league_db):
    assert load_games(con, ACB, league_db) == {"season": "2026-27", "teams": 2, "games": 2}
    games = con.execute("SELECT game_uid, home_team_id, away_team_id, home_score, status "
                        "FROM games ORDER BY 1").fetchall()
    assert games == [("acb:105410", "acb:14", "acb:4", 89, "final"),
                     ("acb:105420", "acb:4", "acb:14", None, "scheduled")]
    aliases = {a for (a,) in con.execute("SELECT alias FROM team_aliases WHERE team_id = 'acb:14'").fetchall()}
    assert aliases == {"Kosner Baskonia", "Baskonia Vitoria-Gasteiz", "Baskonia"}
    load_games(con, ACB, league_db)  # a re-run changes nothing
    assert con.execute("SELECT count(*) FROM games").fetchone()[0] == 2
    # a new alias in the config reaches a team that games already reference
    load_games(con, dataclasses.replace(ACB, aliases={"14": ["Baskonia", "Vitoria"]}), league_db)
    assert con.execute("SELECT count(*) FROM team_aliases WHERE alias = 'Vitoria'").fetchone()[0] == 1


def test_box_score_comes_from_the_same_database(con, league_db):
    load_games(con, ACB, league_db)
    box = load_boxscores(con, league_db, ["acb:105410"], ACB)["acb:105410"]
    assert [s["score"] for s in box["sides"]] == [89, 76]
    assert ACB.official_url(box["game_id"]).endswith("/partidos/105410/estadisticas")
    gbl = League(code="gbl", official_game_url="https://www.esake.gr/el/action/EsakegameView?idgame={game_id:08X}")
    assert gbl.official_url(0x2FCC32F1).endswith("idgame=2FCC32F1")
    assert League(code="aba", official_game_url="").official_url(1) is None


def test_box_score_withheld_when_clubs_disagree(con, league_db):
    load_games(con, ACB, league_db)
    con.execute("UPDATE teams SET bbl_id = '999' WHERE team_id = 'acb:14'")
    assert load_boxscores(con, league_db, ["acb:105410"], ACB) == {}


def test_prompt_and_checks_follow_the_source_language():
    prompt = build_prompt([("Marca", None, "Texto")], league=ACB)
    assert "Spanish professional basketball (Liga Endesa)" in prompt
    assert "Spanish-language source" in prompt and "sin título" in prompt
    english = "Baskonia beat Unicaja 89-76 in the quarterfinal at home. " * 5
    assert validate("Baskonia win", english, ["89 76 cuartos de final"], ACB)["failed"] == []
    spanish = "Baskonia ganó el partido con una victoria y los aficionados del equipo. " * 5
    assert "english" in validate("Baskonia gana", spanish, ["89 76"], ACB)["failed"]
    assert "round_names" in validate("Baskonia win", english, ["89 76"], ACB)["failed"]


def test_relevance_uses_league_keywords_and_language():
    regional = Source(id="sur", publisher="Diario Sur", type="regional_commercial",
                      status="ready", language="es")
    assert is_candidate(regional, "El Unicaja de baloncesto gana", league=ACB)
    assert not is_candidate(regional, "El Málaga CF gana en fútbol", league=ACB)
    assert is_promo("Abonos a la venta para la temporada", language="es")
    assert not is_promo("Abonos a la venta", language="de")
    assert not is_promo("Season tickets on sale", language="en")  # no English lexicon


def test_articles_are_tagged_through_loaded_aliases(con, league_db):
    from datetime import datetime
    from bbl_newsletter.cluster import tag_articles
    load_games(con, ACB, league_db)
    con.execute("INSERT INTO sources VALUES ('sur', 'Diario Sur', 'regional_commercial', NULL, 1)")
    con.execute("INSERT INTO articles (article_id, content_hash, source_id, url, title, published_at, "
                "observed_at, language) VALUES ('a1', 'h1', 'sur', 'https://x/1', "
                "'Baskonia vence al Unicaja', ?, ?, 'es')", [datetime(2026, 11, 9), datetime(2026, 11, 9)])
    tag_articles(con, datetime(2026, 11, 1))
    tagged = con.execute("SELECT team_id FROM article_teams ORDER BY 1").fetchall()
    assert tagged == [("acb:14",), ("acb:4",)]


def test_cli_schedule_follows_the_lake_pointer(tmp_path, league_db, capsys):
    import json
    import shutil
    from bbl_newsletter.__main__ import main
    lake = tmp_path / "lake"
    lake.mkdir()
    shutil.copy(league_db, lake / "20261114T080000Z-abc.db")
    (lake / "current.json").write_text(json.dumps({"database": "20261114T080000Z-abc.db"}))
    shutil.copy(SCHEMA, tmp_path / "schema.sql")
    shutil.copy(Path(__file__).parent / "fixtures" / "registry_small.yaml", tmp_path / "registry.yaml")
    cfg = tmp_path / "config_acb.yaml"
    cfg.write_text(
        "registry: registry.yaml\nschema: schema.sql\ndb: data/acb.duckdb\ndata_dir: data\n"
        "site_dir: site/acb\nboxscore_database: lake/current.json\n"
        "league: {code: acb, source_language: es, competitions: {acb: ACB}, games_source: league_db,\n"
        "         player_identity_column: acb_player_id}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "schedule"]) == 0
    assert json.loads(capsys.readouterr().out)["schedule"]["games"] == 2
