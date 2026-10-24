from pathlib import Path

import pytest

from bbl_newsletter import db
from bbl_newsletter.cluster import cluster, jaccard, _shingles

from datetime import datetime

from helpers import (
    EDITION, TIPOFF, INJURY_DE, SPONSOR_DE, add_article, seed_articles,
    seed_base,
)

SCHEMA = Path(__file__).parents[1] / "schema.sql"


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_schema(c, SCHEMA)
    seed_base(c)
    yield c
    c.close()


@pytest.fixture()
def seeded(con, tmp_path):
    return seed_articles(con, tmp_path)


def test_shingle_similarity_separates_stories():
    assert jaccard(_shingles(INJURY_DE), _shingles(INJURY_DE + " (dpa)")) > 0.5
    assert jaccard(_shingles(INJURY_DE), _shingles(SPONSOR_DE)) < 0.1


def test_tagging(con, seeded):
    report = cluster(con, EDITION)
    teams = dict(con.execute(
        "SELECT article_id, list(team_id ORDER BY team_id) "
        "FROM article_teams GROUP BY article_id").fetchall())
    assert sorted(teams[seeded["recap_club"]]) == ["bayern", "jena"]
    assert teams[seeded["injury"]] == ["bayern"]
    assert seeded["sponsor"] in teams  # 'FC Bayern Basketball' alias hit


def test_game_attachment(con, seeded):
    cluster(con, EDITION)
    rows = dict(con.execute(
        "SELECT article_id, reference FROM article_games").fetchall())
    assert rows[seeded["recap_club"]] == "recap"
    assert rows[seeded["recap_regional"]] == "recap"
    assert rows[seeded["preview"]] == "preview"
    assert seeded["injury"] not in rows  # one team only


def test_stories(con, seeded):
    cluster(con, EDITION)
    stories = con.execute(
        """
        SELECT s.story_id, s.kind, s.game_uid,
               list(sa.article_id ORDER BY sa.article_id)
        FROM stories s JOIN story_articles sa USING (story_id)
        GROUP BY ALL
        """).fetchall()
    by_kind = {}
    for sid, kind, guid, members in stories:
        by_kind.setdefault(kind, []).append((guid, sorted(members)))

    # one game story with all three game articles
    assert len(by_kind["game"]) == 1
    guid, members = by_kind["game"][0]
    assert guid == "bbl:1"
    assert members == sorted([seeded["recap_club"], seeded["recap_regional"], seeded["preview"]])

    # injury + syndicated copy merged; sponsor separate
    news = [set(m) for _, m in by_kind["team_news"]]
    assert {seeded["injury"], seeded["injury_copy"]} in news
    assert {seeded["sponsor"]} in news

    # euroleague article classified
    assert [set(m) for _, m in by_kind["euroleague"]] == [{seeded["euroleague"]}]


def test_generic_state_name_does_not_tag_team(con, tmp_path, seeded):
    # a BR24-style politics article mentioning only "Bayern" (the state)
    aid = add_article(con, tmp_path, "politik", "regional",
        "Streit in der Landespolitik",
        "In Bayern wird weiter über die Reform gestritten. Der Freistaat "
        "Bayern plant nach Angaben der Staatsregierung neue Investitionen, "
        "wie am Montag in München bekannt wurde. Die Opposition in Bayern "
        "kritisierte die Pläne scharf und forderte Nachbesserungen im "
        "kommenden Haushalt des Freistaats.",
        datetime(2026, 10, 12, 13, 0))
    cluster(con, EDITION)
    tags = con.execute(
        "SELECT count(*) FROM article_teams WHERE article_id = ?", [aid]
    ).fetchone()[0]
    assert tags == 0  # "Bayern" alone is the state, not the team


def test_alias_does_not_match_inside_longer_word(con, tmp_path, seeded):
    # "alba" tagged a nordkurier Albanien politics story (observed 2026-10-17)
    con.execute(
        "INSERT INTO teams VALUES ('alba', '20', 'ALBA Berlin', 'Berlin', ['alba'])"
    )
    aid = add_article(con, tmp_path, "albanien", "regional",
        "Proteste gegen Regierung in Albanien weiten sich aus",
        "Die Proteste in Albanien gegen die Regierung dauern an. In Tirana "
        "gingen am Sonntag erneut tausende Menschen auf die Strasse, wie "
        "Beobachter berichteten. Die Opposition fordert Neuwahlen.",
        datetime(2026, 10, 12, 12, 0))
    # genitive-s still tags
    aid2 = add_article(con, tmp_path, "albas-sieg", "regional",
        "Albas Sieg in eigener Halle",
        "Albas Sieg am Sonntag war deutlich, das Team gewann vor "
        "heimischem Publikum klar und bleibt damit ungeschlagen.",
        datetime(2026, 10, 12, 12, 30))
    cluster(con, EDITION)
    assert con.execute(
        "SELECT count(*) FROM article_teams WHERE article_id = ?", [aid]
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT team_id FROM article_teams WHERE article_id = ?", [aid2]
    ).fetchall() == [("alba",)]


def test_late_recap_joins_same_game_story(con, tmp_path, seeded):
    # A recap arriving two days after tipoff (the newscenter pattern,
    # observed 2026-10-21) must join the EXISTING game story, not start a
    # new one, so the composite thickens instead of the game splitting.
    from datetime import timedelta
    from helpers import RECAP_DE

    cluster(con, EDITION)
    story1, members1 = con.execute(
        "SELECT s.story_id, count(*) FROM stories s "
        "JOIN story_articles sa USING (story_id) "
        "WHERE s.kind = 'game' GROUP BY 1").fetchone()
    late = add_article(con, tmp_path, "late-official", "regional",
        "Spielbericht: Bayern bezwingt Jena",
        RECAP_DE + " Der offizielle Spielbericht der Liga.",
        TIPOFF + timedelta(hours=60))
    cluster(con, EDITION + timedelta(days=2))
    games = con.execute(
        "SELECT s.story_id, count(*) FROM stories s "
        "JOIN story_articles sa USING (story_id) "
        "WHERE s.kind = 'game' GROUP BY 1").fetchall()
    assert games == [(story1, members1 + 1)]
    assert con.execute(
        "SELECT count(*) FROM story_articles WHERE article_id = ?", [late]
    ).fetchone()[0] == 1


def test_rerun_idempotent(con, seeded):
    cluster(con, EDITION)
    counts1 = con.execute(
        "SELECT (SELECT count(*) FROM stories), (SELECT count(*) FROM story_articles), "
        "(SELECT count(*) FROM article_teams), (SELECT count(*) FROM article_games)"
    ).fetchone()
    cluster(con, EDITION)
    counts2 = con.execute(
        "SELECT (SELECT count(*) FROM stories), (SELECT count(*) FROM story_articles), "
        "(SELECT count(*) FROM article_teams), (SELECT count(*) FROM article_games)"
    ).fetchone()
    assert counts1 == counts2


def test_ambiguous_double_header_not_attached(con, tmp_path, seeded):
    # a second game of the same pair at the same tipoff: exact tie, no link
    con.execute(
        "INSERT INTO seasons VALUES ('pokal-2026-27', 'pokal', '2026-27', NULL, NULL)")
    con.execute(
        "INSERT INTO games VALUES ('pokal:2', 'pokal-2026-27', NULL, ?, "
        "'bayern', 'jena', NULL, NULL, 'scheduled')", [TIPOFF])
    cluster(con, EDITION)
    assert con.execute("SELECT count(*) FROM article_games").fetchone()[0] == 0


def test_stale_articles_never_tagged_or_clustered(con, seeded, tmp_path):
    # An article stored stale (no extractable publish date) must not reach
    # tags, game attachment or stories even when its text matches a team
    # and a game (observed 2026-10-18: dateless backlog flooded the edition).
    from helpers import RECAP_DE

    stale_aid = add_article(con, tmp_path, "stale-recap", "club",
                            "Bayern schlägt Jena (altes Archiv)", RECAP_DE,
                            datetime(2026, 10, 11, 22, 0), stale=True)
    cluster(con, EDITION)
    assert con.execute(
        "SELECT count(*) FROM article_teams WHERE article_id = ?", [stale_aid]
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM article_games WHERE article_id = ?", [stale_aid]
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM story_articles WHERE article_id = ?", [stale_aid]
    ).fetchone()[0] == 0
