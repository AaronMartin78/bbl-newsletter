from pathlib import Path

import duckdb
import pytest

from bbl_newsletter import db
from bbl_newsletter.registry import Registry

PROJECT_ROOT = Path(__file__).parents[1]
SCHEMA = PROJECT_ROOT / "schema.sql"
REAL_REGISTRY = PROJECT_ROOT / "newsletter_sources.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "registry_small.yaml"


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "test.duckdb")
    db.init_schema(c, SCHEMA)
    yield c
    c.close()


def test_schema_creates_all_tables(con):
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    expected = {
        "sources", "source_state", "teams", "seasons", "games",
        "articles", "article_teams", "article_games",
        "stories", "story_articles", "summaries", "translations",
        "editions", "edition_items", "runs", "run_sources",
    }
    assert expected <= tables


def test_init_schema_idempotent(con):
    db.init_schema(con, SCHEMA)  # second run must not raise


def test_sync_sources_upserts(con):
    reg = Registry.load(FIXTURE)
    db.sync_sources(con, reg)
    db.sync_sources(con, reg)  # idempotent
    n = con.execute("SELECT count(*) FROM sources").fetchone()[0]
    assert n == len(reg.sources)
    states = con.execute("SELECT count(*) FROM source_state").fetchone()[0]
    assert states == n


def test_article_revisions_coexist_and_duplicates_rejected(con):
    reg = Registry.load(FIXTURE)
    db.sync_sources(con, reg)
    insert = """
        INSERT INTO articles (article_id, content_hash, source_id, url, title,
                              published_at, observed_at, language, body_path)
        VALUES (?,?,?,?,?,?,?,?,?)
    """
    row = ["a" * 64, "h1", "hagen", "https://phoenix-hagen.de/x", "t", None, "2026-10-12 08:00:00", "de", None]
    con.execute(insert, row)
    row2 = list(row); row2[1] = "h2"  # same article, new content hash = revision
    con.execute(insert, row2)
    with pytest.raises(duckdb.ConstraintException):
        con.execute(insert, row)


def test_duplicate_edition_impossible(con):
    con.execute("INSERT INTO editions VALUES (DATE '2026-10-12', 'draft', NOW(), NULL)")
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO editions VALUES (DATE '2026-10-12', 'draft', NOW(), NULL)")


@pytest.mark.skipif(not REAL_REGISTRY.exists(), reason="real registry not present")
def test_real_registry_loads_and_syncs(con):
    reg = Registry.load(REAL_REGISTRY)
    assert reg.version >= 1
    assert {"bbl_newscenter", "bayern", "hessenschau"} <= {s.id for s in reg.sources}
    db.sync_sources(con, reg)
    usable = {s.id for s in reg.usable()}
    assert "stuttgarter_zeitung" not in usable
    assert "hagen" in usable
