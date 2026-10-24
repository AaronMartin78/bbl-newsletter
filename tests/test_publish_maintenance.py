import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from bbl_newsletter import db
from bbl_newsletter.cluster import cluster
from bbl_newsletter.maintenance import cleanup_listing_articles
from bbl_newsletter.publish import PublishError, publish_edition
from bbl_newsletter.registry import Registry

from helpers import EDITION, seed_articles, seed_base, add_article

SCHEMA = Path(__file__).parents[1] / "schema.sql"


# ------------------------------------------------------------------ publish
def _run(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True)


@pytest.fixture()
def website(tmp_path):
    """A bare 'origin' and a working clone with one commit on main."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    clone = tmp_path / "website"
    subprocess.run(["git", "clone", str(bare), str(clone)],
                   check=True, capture_output=True)
    _run(clone, "config", "user.email", "pipeline@test")
    _run(clone, "config", "user.name", "pipeline")
    (clone / "README.md").write_text("site\n", encoding="utf-8")
    _run(clone, "add", "README.md")
    _run(clone, "commit", "-m", "init")
    _run(clone, "push", "origin", "main")
    return bare, clone


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_schema(c, SCHEMA)
    yield c
    c.close()


def test_publish_pushes_edition_branch(con, website, tmp_path):
    bare, clone = website
    con.execute("INSERT INTO editions VALUES (DATE '2026-10-13', 'draft', NOW(), NULL)")
    qmd = tmp_path / "2026-10-13.qmd"
    qmd.write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")

    report = publish_edition(con, date(2026, 10, 13), qmd, clone)
    assert report["status"] == "pushed" and len(report["commit"]) == 40

    log = subprocess.run(
        ["git", "-C", str(bare), "log", "--oneline", "editions"],
        check=True, capture_output=True, text=True).stdout
    assert "BBL Daily 2026-10-13 (automated)" in log

    status, commit = con.execute(
        "SELECT status, git_commit FROM editions WHERE edition_date = DATE '2026-10-13'"
    ).fetchone()
    assert status == "pushed" and commit == report["commit"]

    # retry with identical content: no duplicate commit
    again = publish_edition(con, date(2026, 10, 13), qmd, clone)
    assert again["status"] == "unchanged"

    # second day stacks on the same branch
    qmd2 = tmp_path / "2026-10-14.qmd"
    qmd2.write_text("---\ntitle: y\n---\nbody2\n", encoding="utf-8")
    con.execute("INSERT INTO editions VALUES (DATE '2026-10-14', 'draft', NOW(), NULL)")
    assert publish_edition(con, date(2026, 10, 14), qmd2, clone)["status"] == "pushed"


def test_publish_refuses_missing_inputs(con, tmp_path):
    with pytest.raises(PublishError, match="missing"):
        publish_edition(con, date(2026, 10, 13), tmp_path / "nope.qmd", tmp_path)


# ------------------------------------------------------------------ cleanup
@pytest.fixture()
def registry_small():
    return Registry.model_validate({
        "version": 1,
        "defaults": {"user_agent": "t"},
        "sources": [
            {"id": "club", "publisher": "C", "type": "club",
             "method": "html_listing", "url": "https://club.example/news",
             "status": "ready"},
            {"id": "regional", "publisher": "R", "type": "regional_public",
             "method": "rss", "feed": "https://regional.example/sport/index.rss",
             "status": "conditional"},
        ],
    })


def test_cleanup_removes_listing_articles_and_their_stories(
        con, tmp_path, registry_small):
    seed_base(con)
    arts = seed_articles(con, tmp_path)
    junk = add_article(con, tmp_path, "junk", "club",
                       None, "FC Bayern Basketball " + "Schlagzeile " * 60,
                       datetime(2026, 10, 12, 7, 0))
    con.execute("UPDATE articles SET url = 'https://club.example/news/newsarchiv' "
                "WHERE article_id = ?", [junk])
    cluster(con, EDITION)
    assert con.execute(
        "SELECT count(*) FROM story_articles WHERE article_id = ?", [junk]
    ).fetchone()[0] >= 1

    report = cleanup_listing_articles(con, registry_small)
    assert report["articles_removed"] == 1
    assert report["stories_removed"] >= 1
    assert con.execute(
        "SELECT count(*) FROM articles WHERE article_id = ?", [junk]
    ).fetchone()[0] == 0
    # real articles survive
    assert con.execute(
        "SELECT count(*) FROM articles WHERE article_id = ?", [arts["injury"]]
    ).fetchone()[0] == 1
    # re-cluster rebuilds stories without the junk
    cluster(con, EDITION)
    assert con.execute("SELECT count(*) FROM stories").fetchone()[0] > 0
    assert con.execute(
        "SELECT count(*) FROM story_articles WHERE article_id = ?", [junk]
    ).fetchone()[0] == 0


def test_cleanup_noop_on_clean_db(con, tmp_path, registry_small):
    seed_base(con)
    seed_articles(con, tmp_path)
    report = cleanup_listing_articles(con, registry_small)
    assert report == {"articles_removed": 0, "stories_removed": 0,
                      "summaries_removed": 0}


# -------------------------------------------------------------------- retag
def test_retag_removes_stale_tags_and_their_stories(con, tmp_path):
    """Tags created under old rules (bare 'Bayern' on BR24 general news)
    survive re-clustering because tag inserts are additive. retag_articles
    must purge them and the stories/summaries built on them, leaving valid
    stories untouched (observed live 2026-10-14)."""
    from datetime import datetime

    from bbl_newsletter.maintenance import retag_articles

    seed_base(con)
    seed_articles(con, tmp_path)
    off_topic = add_article(
        con, tmp_path, "dackel", "regional", "Dackel beißt Seniorin",
        "Eine 70-jährige Frau wurde in Neutraubling im Landkreis Regensburg "
        "von einem Rauhaardackel gebissen und musste drei Tage im "
        "Krankenhaus behandelt werden, teilte die Polizei in Bayern mit.",
        datetime(2026, 10, 12, 9, 30),
    )
    # stale tag as the pre-fix tagger would have written it
    con.execute("INSERT INTO article_teams VALUES (?, 'bayern')", [off_topic])
    cluster(con, EDITION)
    assert con.execute(
        "SELECT count(*) FROM story_articles WHERE article_id = ?", [off_topic]
    ).fetchone()[0] > 0
    con.execute("""
        INSERT INTO summaries (summary_id, story_id, input_hash, model,
                               prompt_version, created_at, text_en, status)
        SELECT 'sum-stale', story_id, 'h', 'm', 'v2', NOW(), 'junk body',
               'candidate'
        FROM story_articles WHERE article_id = ? LIMIT 1
    """, [off_topic])

    stories_before = con.execute("SELECT count(*) FROM stories").fetchone()[0]
    report = retag_articles(con)

    assert report["stale_tags_removed"] >= 1
    assert report["stories_removed"] >= 1
    assert con.execute(
        "SELECT count(*) FROM story_articles WHERE article_id = ?", [off_topic]
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM summaries WHERE summary_id = 'sum-stale'"
    ).fetchone()[0] == 0
    # valid stories survive with their tags
    assert con.execute("SELECT count(*) FROM stories").fetchone()[0] \
        == stories_before - report["stories_removed"]
    assert con.execute(
        "SELECT count(*) FROM article_teams WHERE team_id = 'bayern'"
    ).fetchone()[0] > 0


def test_exclude_article_is_permanent(con, tmp_path):
    """Excluding by URL kills the article's non-game stories and survives a
    re-cluster: the junk cannot come back (editorial kill switch)."""
    from bbl_newsletter.maintenance import exclude_articles

    seed_base(con)
    seed_articles(con, tmp_path)
    cluster(con, EDITION)
    url = "https://club.example/sponsor"
    assert con.execute(
        "SELECT count(*) FROM story_articles sa JOIN articles a USING (article_id) "
        "WHERE a.url = ?", [url]
    ).fetchone()[0] == 1

    report = exclude_articles(con, [url, "https://nowhere.example/x"], reason="stale")
    assert report["excluded"] == 1 and report["stories_removed"] == 1
    assert report["not_found"] == ["https://nowhere.example/x"]

    cluster(con, EDITION)  # re-cluster must not resurrect it
    assert con.execute(
        "SELECT count(*) FROM story_articles sa JOIN articles a USING (article_id) "
        "WHERE a.url = ?", [url]
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM article_teams at2 JOIN articles a USING (article_id) "
        "WHERE a.url = ?", [url]
    ).fetchone()[0] == 0
    # untouched stories survive
    assert con.execute("SELECT count(*) FROM stories").fetchone()[0] > 0
