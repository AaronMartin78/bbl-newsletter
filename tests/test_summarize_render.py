import json
from pathlib import Path

import pytest

from bbl_newsletter import db
from bbl_newsletter.cluster import cluster
from bbl_newsletter.render import render_edition
from bbl_newsletter.summarize import (
    parse_response, summarize_pending, summarize_story, validate,
)

from helpers import EDITION, INJURY_DE, RECAP_DE, seed_articles, seed_base

SCHEMA = Path(__file__).parents[1] / "schema.sql"
MODEL = "test-model:1"

GOOD_GAME = {
    "headline": "Bayern beat Jena 89:76 behind Edwards",
    "summary": (
        "FC Bayern Basketball defeated Science City Jena 89:76 at the BMW Park "
        "in front of 6500 spectators. A dominant third quarter decided the "
        "game, with Jena shooting only 38 percent from the field, a defensive "
        "effort coach Herbert praised afterwards. Carsen Edwards led all "
        "scorers with 21 points. Before the game, regional coverage had "
        "flagged Jena's roster problems, which showed over the full forty "
        "minutes as Bayern pulled away late."
    ),
}

GOOD_TOPIC = {
    "headline": "Edwards out for several weeks",
    "summary": (
        "FC Bayern Basketball guard Carsen Edwards will miss several weeks "
        "with an ankle injury suffered in practice, the club announced. "
        "Surgery will not be required, but a return is considered realistic "
        "in November at the earliest. The club statement was carried by "
        "regional outlets as well, which reported the same timeline for the "
        "guard's recovery and no further complications."
    ),
}


def good_generator(prompt: str) -> str:
    return json.dumps(GOOD_GAME if "GAME:" in prompt else GOOD_TOPIC)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_schema(c, SCHEMA)
    seed_base(c)
    yield c
    c.close()


@pytest.fixture()
def clustered(con, tmp_path):
    articles = seed_articles(con, tmp_path)
    cluster(con, EDITION)
    return articles


# ------------------------------------------------------------- unit checks
def test_prompt_carries_person_identity_rules():
    # mitigation for person conflation / role garble (problems 1-2, 2026-10-14)
    from bbl_newsletter.summarize import PROMPT_VERSION, build_prompt

    prompt = build_prompt([("Pub", "Titel", "Textkörper")])
    assert "separate individual" in prompt
    assert "coach with the team the sources give them" in prompt
    assert PROMPT_VERSION == "v3"  # identity rules force a re-summarize


def test_parse_response_tolerates_fencing():
    raw = "```json\n" + json.dumps(GOOD_TOPIC) + "\n```"
    headline, summary = parse_response(raw)
    assert headline == GOOD_TOPIC["headline"]


def test_parse_response_strips_think_block():
    raw = ("<think> We need to produce a JSON object {with braces} and "
           "reasoning... </think>\n" + json.dumps(GOOD_TOPIC))
    headline, summary = parse_response(raw)
    assert headline == GOOD_TOPIC["headline"]


def test_parse_response_unterminated_think_raises():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unterminated"):
        parse_response("<think> endless reasoning that got cut off")


def test_validate_rejects_fabricated_number():
    checks = validate("Bayern win 102:76", GOOD_GAME["summary"], [RECAP_DE])
    assert "numbers_traceable" in checks["failed"]


def test_validate_rejects_german_output():
    checks = validate(
        "Bayern gewinnt gegen Jena",
        "Der FC Bayern hat gegen Jena gewonnen und bleibt damit nicht zu "
        "stoppen. " * 6,
        [RECAP_DE],
    )
    assert "english" in checks["failed"]


def test_parse_response_salvages_real_v3_failures():
    # 12 verbatim llama3.1:8b outputs from the 2026-10-17 run (99/120
    # rejects): quoted headline, unquoted summary value, no closing brace
    import re as _re
    from pathlib import Path as _Path

    text = (_Path(__file__).parent / "fixtures" /
            "parse_failures_2026-10-17.txt").read_text(encoding="utf-8")
    records = [r.strip() for r in
               _re.split(r"(?m)^=== RECORD [0-9a-f]+ .*===$", text)[1:]
               if r.strip()]
    assert len(records) == 12
    for raw in records:
        headline, summary = parse_response(raw)
        assert headline and not headline.startswith('"')
        assert len(summary.split()) >= 30


def test_response_schema_requires_both_fields():
    from bbl_newsletter.summarize import RESPONSE_SCHEMA

    assert set(RESPONSE_SCHEMA["required"]) == {"headline", "summary"}
    assert set(RESPONSE_SCHEMA["properties"]) == {"headline", "summary"}


def test_parse_response_salvages_unquoted_summary():
    raw = ('{"headline": "SKYLINERS edge Towers 89:78",  "summary":  '
           "The SKYLINERS secured a win over the Towers on Saturday. }")
    headline, summary = parse_response(raw)
    assert headline.startswith("SKYLINERS")
    assert summary.startswith("The SKYLINERS secured")
    assert not summary.rstrip().endswith("}")


def test_validate_number_format_tolerance():
    source = "NINERS gewinnen 94:93 nach Verlängerung. Saison 2026/27 vor 5.500 Fans."
    checks = validate(
        "Niners win thriller 94-93",
        "Niners Chemnitz won the overtime game 94-93 in front of 5500 fans, "
        "their first home win of the 2026/2027 season. The crowd of 5500 saw "
        "a dramatic finish decided only in the extra period, a strong start "
        "for the hosts this autumn according to the club's own report.",
        [source],
    )
    assert "numbers_traceable" not in checks["failed"]
    bad = validate("Niners win 102-93", "They won 102-93. " * 10, [source])
    assert "numbers_traceable" in bad["failed"]


def test_validate_rejects_truncated_summary():
    # a cut-off model output is valid JSON and passed every check
    # (observed 2026-10-20: "...who described it as their")
    truncated = GOOD_GAME["summary"].rsplit(".", 2)[0] + " who described it as their"
    checks = validate(GOOD_GAME["headline"], truncated, [RECAP_DE])
    assert "complete_sentence" in checks["failed"]


def test_validate_rejects_wrong_round_name():
    # Achtelfinale rendered as "quarterfinals" (owner's read, 2026-10-20)
    source = "Endstation Achtelfinale: Löwen verlieren deutlich gegen die NINERS."
    wrong = validate(
        "Braunschweig cup run ends",
        "Loewen Braunschweig were eliminated in the quarterfinals of the cup, "
        "losing clearly to the NINERS. The team never found its rhythm and "
        "trailed from the opening minutes, a defeat the coach called deserved "
        "while pointing to the young roster's inexperience on the big stage.",
        [source],
    )
    assert "round_names" in wrong["failed"]
    right = validate(
        "Braunschweig cup run ends",
        "Loewen Braunschweig were eliminated in the round of 16 of the cup, "
        "losing clearly to the NINERS. The team never found its rhythm and "
        "trailed from the opening minutes, a defeat the coach called deserved "
        "while pointing to the young roster's inexperience on the big stage.",
        [source],
    )
    assert "round_names" not in right["failed"]


def test_validate_accepts_good_summary():
    checks = validate(GOOD_GAME["headline"], GOOD_GAME["summary"],
                      [RECAP_DE, "Vorschau 18:30 40 Minuten"])
    assert checks["failed"] == []


# ------------------------------------------------------------ stage checks
def test_summarize_limit_prefers_game_stories(con, clustered):
    results = summarize_pending(con, good_generator, MODEL, limit=1)
    assert len(results) == 1
    kind = con.execute(
        "SELECT st.kind FROM summaries su JOIN stories st USING (story_id)"
    ).fetchone()[0]
    assert kind == "game"


def test_summarize_stories(con, clustered):
    results = summarize_pending(con, good_generator, MODEL)
    statuses = {r.status for r in results}
    assert statuses == {"candidate"}
    # deterministic: re-run produces no new rows
    again = summarize_pending(con, good_generator, MODEL)
    assert {r.status for r in again} == {"exists"}
    n = con.execute("SELECT count(*) FROM summaries").fetchone()[0]
    assert n == len(results)


def test_rejected_summary_recorded_not_published(con, clustered, tmp_path):
    def bad_generator(prompt: str) -> str:
        return json.dumps({
            "headline": "Bayern crush Jena 102:54",
            "summary": "FC Bayern Basketball crushed Science City Jena "
                       "102:54 in a record rout. " * 5,
        })

    results = summarize_pending(con, bad_generator, MODEL)
    assert all(r.status == "rejected" for r in results)
    assert render_edition(con, EDITION, tmp_path / "site") is None


def test_parse_failure_is_rejected(con, clustered):
    results = summarize_pending(con, lambda p: "I cannot answer that.", MODEL)
    assert all(r.status == "rejected" for r in results)
    rows = con.execute(
        "SELECT validation FROM summaries").fetchall()
    assert all("parse" in json.loads(v[0])["failed"] for v in rows)


# ------------------------------------------------------------------ render
def test_render_includes_previous_day_and_dedupes(con, clustered, tmp_path):
    from datetime import datetime, timedelta
    summarize_pending(con, good_generator, MODEL,
                      now=datetime.combine(EDITION, datetime.min.time()))
    # same stories clustered again under tomorrow's date = duplicate member sets
    tomorrow = EDITION + timedelta(days=1)
    cluster(con, tomorrow)
    summarize_pending(con, good_generator, MODEL,
                      now=datetime.combine(tomorrow, datetime.min.time()))
    path = render_edition(con, tomorrow, tmp_path / "site")
    assert path is not None  # yesterday's and today's stories both in window
    text = path.read_text(encoding="utf-8")
    # the game story's member set exists under both dates but renders once
    assert text.count(GOOD_GAME["headline"]) == 1
    # one item per distinct member set: game, injury pair, sponsor, euroleague
    assert text.count("### ") == 4


def test_rendered_story_repeats_only_when_grown(con, clustered, tmp_path):
    # 12 of 25 items in the 2026-10-21 edition repeated the previous day:
    # the window re-listed every story for 2-3 days. An edition must print
    # a story once, and again only when it gained sources.
    from datetime import datetime, timedelta
    from helpers import RECAP_DE, TIPOFF, add_article

    summarize_pending(con, good_generator, MODEL,
                      now=datetime.combine(EDITION, datetime.min.time()))
    assert render_edition(con, EDITION, tmp_path / "site") is not None

    # next day, nothing new: re-clustering mints duplicate stories and
    # summaries for the same inputs, but the edition prints none of them
    tomorrow = EDITION + timedelta(days=1)
    cluster(con, tomorrow)
    summarize_pending(con, good_generator, MODEL,
                      now=datetime.combine(tomorrow, datetime.min.time()))
    assert render_edition(con, tomorrow, tmp_path / "site") is None

    # a late recap thickens the game story: it re-lists, and only it
    day_after = EDITION + timedelta(days=2)
    add_article(con, tmp_path, "late-official", "regional",
                "Spielbericht: Bayern bezwingt Jena",
                RECAP_DE + " Der offizielle Spielbericht der Liga.",
                TIPOFF + timedelta(hours=60))
    cluster(con, day_after)
    summarize_pending(con, good_generator, MODEL,
                      now=datetime.combine(day_after, datetime.min.time()))
    path = render_edition(con, day_after, tmp_path / "site")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert text.count("### ") == 1
    assert GOOD_GAME["headline"] in text
    assert "late-official" in text  # the new source is cited
    assert "(updated with new sources)" in text  # reader sees why it is back


def test_render_edition(con, clustered, tmp_path):
    from datetime import datetime
    summarize_pending(con, good_generator, MODEL,
                      now=datetime.combine(EDITION, datetime.min.time()))
    path = render_edition(con, EDITION, tmp_path / "site")
    assert path is not None and path.name == "2026-10-12.qmd"
    text = path.read_text(encoding="utf-8")

    assert 'title: "BBL Daily - 12 October 2026"' in text
    assert "## Games" in text and "## European Cups" in text
    assert GOOD_GAME["headline"] in text and GOOD_TOPIC["headline"] in text
    # the reader sees which night the game was, first listing unmarked
    assert "*Played Sunday, 11 October 2026*" in text
    assert "(updated with new sources)" not in text
    # citations come from the DB, one per member article of the game story
    assert "https://club.example/recap-club" in text
    assert "https://regional.example/preview" in text
    # no German bodies leak into the edition
    assert "gewonnen" not in text

    row = con.execute(
        "SELECT status FROM editions WHERE edition_date = ?", [EDITION]
    ).fetchone()
    assert row == ("draft",)

    # re-render: identical file, no duplicate items
    items_before = con.execute("SELECT count(*) FROM edition_items").fetchone()[0]
    path2 = render_edition(con, EDITION, tmp_path / "site")
    assert path2.read_text(encoding="utf-8") == text
    items_after = con.execute("SELECT count(*) FROM edition_items").fetchone()[0]
    assert items_before == items_after
