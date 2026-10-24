"""Cross-check: the pipeline's rendered edition must pass the website CI
validator (Newsletter/website/scripts/check_edition.py) offline."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bbl_newsletter import db
from bbl_newsletter.cluster import cluster
from bbl_newsletter.render import render_edition
from bbl_newsletter.summarize import summarize_pending

from helpers import EDITION, seed_articles, seed_base
from test_summarize_render import GOOD_GAME, GOOD_TOPIC, good_generator

SCHEMA = Path(__file__).parents[1] / "schema.sql"
CHECKER = Path(__file__).parents[1] / "tools" / "check_edition.py"


def run_checker(*files):
    return subprocess.run(
        [sys.executable, str(CHECKER), *map(str, files)],
        capture_output=True, text=True,
    )


@pytest.mark.skipif(not CHECKER.exists(), reason="website scaffold not present")
def test_rendered_edition_passes_ci_checks(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con, SCHEMA)
    seed_base(con)
    seed_articles(con, tmp_path)
    cluster(con, EDITION)
    summarize_pending(con, good_generator, "test-model:1")
    path = render_edition(con, EDITION, tmp_path / "site")
    con.close()

    result = run_checker(path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not CHECKER.exists(), reason="website scaffold not present")
def test_checker_rejects_bad_editions(tmp_path):
    german = tmp_path / "2026-10-13.qmd"
    german.write_text(
        "---\ntitle: \"BBL Daily\"\ndate: 2026-10-13\nlang: en\n---\n\n"
        "## Games\n\n### Bayern gewinnt\n\nBayern hat gegen Jena gewonnen "
        "und bleibt damit nicht zu stoppen, wie der Klub beim Spiel sagte.\n\n"
        "Sources:\n\n- [Club: Artikel](https://club.example/a)\n",
        encoding="utf-8",
    )
    result = run_checker(german)
    assert result.returncode == 1 and "reads as German" in result.stdout

    no_sources = tmp_path / "2026-10-14.qmd"
    no_sources.write_text(
        "---\ntitle: \"BBL Daily\"\ndate: 2026-10-14\nlang: en\n---\n\n"
        "## Games\n\n### A headline\n\nAn English paragraph without any "
        "citations at all.\n",
        encoding="utf-8",
    )
    result = run_checker(no_sources)
    assert result.returncode == 1 and "no Sources" in result.stdout

    wrong_date = tmp_path / "2026-10-15.qmd"
    wrong_date.write_text(
        "---\ntitle: \"BBL Daily\"\ndate: 2026-10-14\nlang: en\n---\n\n"
        "## Games\n\n### A headline\n\nEnglish prose here.\n\n"
        "Sources:\n\n- [Club: Artikel](https://club.example/a)\n",
        encoding="utf-8",
    )
    result = run_checker(wrong_date)
    assert result.returncode == 1 and "!= filename" in result.stdout
