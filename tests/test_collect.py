from datetime import datetime
from pathlib import Path

import httpx
import pytest

from bbl_newsletter import db
from bbl_newsletter.collect import collect_all, collect_source
from bbl_newsletter.fetch import Fetcher
from bbl_newsletter.registry import Registry, Source

from fixtures import pages

SCHEMA = Path(__file__).parents[1] / "schema.sql"
NOW = datetime(2026, 10, 12, 6, 0, 0)

UA = "bbl-newsletter-collector/0.1 (test)"


def handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if path == "/robots.txt":
        return httpx.Response(404)
    # rss club source (full bodies) and rss regional (teaser only)
    if host == "club.example" and path == "/feed/":
        if request.headers.get("If-None-Match") == 'W/"feed-v1"':
            return httpx.Response(304)
        return httpx.Response(
            200, text=pages.wp_feed("club.example", full_body=True),
            headers={"ETag": 'W/"feed-v1"'},
        )
    if host == "regional.example" and path == "/sport/index.rss":
        return httpx.Response(200, text=pages.wp_feed(
            "regional.example", full_body=False,
            titles=("Niners Chemnitz feiern Auswärtssieg", "Derby endet torlos"),
        ))
    # news sitemap source
    if host == "ard.example" and path == "/news.xml":
        return httpx.Response(200, text=pages.news_sitemap("ard.example"))
    # mdr-style sitemap with a /video-* roundup page
    if host == "mdr.example" and path == "/news.xml":
        return httpx.Response(200, text=pages.news_sitemap("mdr.example", video=True))
    # club feed whose first item is a ticket pre-sale announcement
    if host == "promo.example" and path == "/feed/":
        return httpx.Response(200, text=pages.wp_feed(
            "promo.example", full_body=False,
            titles=("Vorverkauf für das Topspiel startet", "Niners gewinnen Auswärtsspiel"),
        ))
    # bbl newscenter listing
    if host == "easycredit-bbl.de" and path == "/newscenter":
        return httpx.Response(200, text=pages.newscenter_listing())
    # any article page
    return httpx.Response(200, text=pages.article_html())


def make_fetcher():
    return Fetcher(user_agent=UA, min_host_interval_s=0.0,
                   transport=httpx.MockTransport(handler))


def src(**kw) -> Source:
    return Source(**kw)


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_schema(c, SCHEMA)
    yield c
    c.close()


@pytest.fixture()
def registry():
    return Registry.model_validate(
        {
            "version": 1,
            "defaults": {
                "user_agent": UA,
                "max_articles_per_source_per_run": 20,
            },
            "sources": [
                {"id": "club", "publisher": "Club", "type": "club",
                 "method": "rss", "feed": "https://club.example/feed/",
                 "status": "ready"},
                {"id": "regional", "publisher": "Regional", "type": "regional_public",
                 "method": "rss", "feed": "https://regional.example/sport/index.rss",
                 "status": "conditional"},
                {"id": "ard", "publisher": "ARD", "type": "regional_public",
                 "method": "sitemap", "sitemap": "https://ard.example/news.xml",
                 "url": "https://ard.example", "status": "ready"},
                {"id": "bbl_newscenter", "publisher": "BBL", "type": "league",
                 "method": "html_listing", "url": "https://easycredit-bbl.de/newscenter",
                 "article_url_pattern": r"https://cms\.easycredit-bbl\.de/de/n/news/",
                 "status": "ready"},
                {"id": "off", "publisher": "Off", "type": "regional_commercial",
                 "url": "https://off.example", "status": "disabled"},
                {"id": "mdr", "publisher": "MDR", "type": "regional_public",
                 "method": "sitemap", "sitemap": "https://mdr.example/news.xml",
                 "url": "https://mdr.example", "status": "ready",
                 "exclude_url_pattern": "/video-"},
                {"id": "promoclub", "publisher": "Promo Club", "type": "club",
                 "method": "rss", "feed": "https://promo.example/feed/",
                 "status": "ready"},
            ],
        }
    )


def _sync(con, registry):
    db.sync_sources(con, registry)


def test_rss_club_uses_feed_body_and_conditional_get(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    club = registry.by_id("club")
    rep = collect_source(con, club, f, tmp_path, now=NOW)
    # both items stored: full-body item without page fetch, teaser item via page
    assert rep.status == "ok" and rep.items_new == 2
    # tracking param stripped in stored url
    urls = [r[0] for r in con.execute("SELECT url FROM articles ORDER BY url").fetchall()]
    assert all("utm_" not in u for u in urls)
    # second run: conditional GET hits 304, nothing new
    rep2 = collect_source(con, club, f, tmp_path, now=NOW)
    assert rep2.status == "not_modified" and rep2.items_new == 0


def test_regional_rss_filters_non_basketball(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    regional = registry.by_id("regional")
    rep = collect_source(con, regional, f, tmp_path, now=NOW)
    assert rep.status == "ok" and rep.items_new == 1
    titles = [t[0] for t in con.execute(
        "SELECT title FROM articles WHERE source_id = 'regional'").fetchall()]
    assert any("Niners" in t for t in titles)      # basketball item kept
    assert all("Derby" not in t for t in titles)   # football item skipped


def test_sitemap_filters_by_date_and_relevance(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    ard = registry.by_id("ard")
    rep = collect_source(con, ard, f, tmp_path, now=NOW)
    assert rep.status == "ok"
    urls = [r[0] for r in con.execute(
        "SELECT url FROM articles WHERE source_id = 'ard'").fetchall()]
    assert any("basketball-bayern-jena" in u for u in urls)
    assert all("fussball" not in u for u in urls)      # relevance filter
    assert all("basketball-alt" not in u for u in urls)  # 3-day window


def test_listing_pattern_and_dedup(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    bbl = registry.by_id("bbl_newscenter")
    rep = collect_source(con, bbl, f, tmp_path, now=NOW)
    assert rep.status == "ok"
    urls = [r[0] for r in con.execute(
        "SELECT url FROM articles WHERE source_id = 'bbl_newscenter' ORDER BY url"
    ).fetchall()]
    assert len(urls) == 3  # duplicate link collapsed, Tabelle link pattern-rejected
    assert any("a=1&b=2" in u for u in urls)  # &amp; entity unescaped


def test_listing_family_pages_never_become_articles(con, registry, tmp_path):
    listing_html = """
    <a href="https://club2.example/news">News</a>
    <a href="https://club2.example/news/">News slash</a>
    <a href="https://club2.example/news/index.html">Index</a>
    <a href="https://club2.example/news/newsarchiv/index.html">Archiv</a>
    <a href="https://club2.example/news?currentPage=2">Page 2</a>
    <a href="https://club2.example/news/seite?tx_news_pi1%5BcurrentPage%5D=3">TYPO3 page</a>
    <a href="https://club2.example/news/echter-artikel-sieg-gegen-bonn">Artikel</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/news" and not request.url.query:
            return httpx.Response(200, text=listing_html)
        return httpx.Response(200, text=pages.article_html())

    src2 = Source(id="club2", publisher="Club2", type="club",
                  method="html_listing", url="https://club2.example/news",
                  status="ready")
    con.execute("INSERT INTO sources VALUES ('club2', 'Club2', 'club', NULL, 1)")
    con.execute("INSERT INTO source_state (source_id) VALUES ('club2')")
    f = Fetcher(user_agent=UA, min_host_interval_s=0.0,
                transport=httpx.MockTransport(handler))
    rep = collect_source(con, src2, f, tmp_path, now=NOW)
    urls = [r[0] for r in con.execute(
        "SELECT url FROM articles WHERE source_id = 'club2'").fetchall()]
    assert urls == ["https://club2.example/news/echter-artikel-sieg-gegen-bonn"]


def test_body_files_written(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    collect_source(con, registry.by_id("club"), f, tmp_path, now=NOW)
    for (path,) in con.execute("SELECT body_path FROM articles").fetchall():
        p = Path(path)
        assert p.exists() and len(p.read_text(encoding="utf-8")) > 300


def test_collect_all_records_run(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    run_id, reports = collect_all(con, registry, f, tmp_path, now=NOW)
    assert {r.source_id for r in reports} == {
        "club", "regional", "ard", "bbl_newscenter", "mdr", "promoclub"
    }
    ok = con.execute("SELECT ok FROM runs WHERE run_id = ?", [run_id]).fetchone()[0]
    assert ok is True
    rows = con.execute(
        "SELECT count(*) FROM run_sources WHERE run_id = ?", [run_id]
    ).fetchone()[0]
    assert rows == 6
    # disabled source never polled
    assert con.execute(
        "SELECT count(*) FROM run_sources WHERE source_id = 'off'"
    ).fetchone()[0] == 0


def test_rerun_is_idempotent(con, registry, tmp_path):
    _sync(con, registry)
    f = make_fetcher()
    collect_all(con, registry, f, tmp_path, now=NOW)
    before = con.execute("SELECT count(*) FROM articles").fetchone()[0]
    f2 = make_fetcher()  # fresh fetcher: no robots cache, feeds re-fetched
    collect_all(con, registry, f2, tmp_path, now=NOW)
    after = con.execute("SELECT count(*) FROM articles").fetchone()[0]
    assert before == after


def test_exclude_url_pattern_skips_video_pages(con, registry, tmp_path):
    # MDR /video-* roundups: thin multi-sport pages, never fetched
    # (owner review problem 3, 2026-10-14)
    _sync(con, registry)
    f = make_fetcher()
    rep = collect_source(con, registry.by_id("mdr"), f, tmp_path, now=NOW)
    assert rep.status == "ok" and rep.items_new == 1
    urls = [u for (u,) in con.execute(
        "SELECT url FROM articles WHERE source_id = 'mdr'").fetchall()]
    assert urls and all("/video-" not in u for u in urls)


def test_promo_titles_filtered_for_club_sources(con, registry, tmp_path):
    # ticket pre-sale posts pass as team news but age badly on a website
    # (owner review problem 5, 2026-10-14); clubs bypass is_candidate,
    # so the promo filter must catch them separately
    _sync(con, registry)
    f = make_fetcher()
    rep = collect_source(con, registry.by_id("promoclub"), f, tmp_path, now=NOW)
    assert rep.status == "ok" and rep.items_new == 1
    titles = [t for (t,) in con.execute(
        "SELECT title FROM articles WHERE source_id = 'promoclub'").fetchall()]
    assert titles == ["Niners gewinnen Auswärtsspiel"]


def test_relevance_tokens_end_at_word_boundary():
    # "alba" prefix-matched "Albanien" (nordkurier politics, 2026-10-17)
    from bbl_newsletter.relevance import is_candidate

    regional = Source(id="r", publisher="R", type="regional_public",
                      status="conditional")
    assert not is_candidate(regional, "Proteste gegen Regierung in Albanien")
    assert is_candidate(regional, "Albas Sieg im Spitzenspiel")
    assert is_candidate(regional, "Die Albatrosse gewinnen erneut")


def test_is_promo_matches_sale_words_only():
    from bbl_newsletter.relevance import is_promo

    assert is_promo("Vorverkauf für das Topspiel startet")
    assert is_promo("Dauerkarten ab sofort im Ticketshop")
    assert is_promo("Großes Gewinnspiel zum Saisonstart")
    assert not is_promo("Niners lösen das Ticket für die Playoffs")
    assert not is_promo("Bayern schlägt Jena 89:76")
    assert not is_promo(None)


def test_listing_path_catches_pagination_segments():
    from bbl_newsletter.discover import _LISTING_PATH

    assert _LISTING_PATH.search("/news/seite/1")       # observed live 2026-10-14
    assert _LISTING_PATH.search("/news/seite/12/")
    assert not _LISTING_PATH.search("/news/seite-an-seite-zum-sieg")


def test_page_date_fills_missing_published_at(con, tmp_path):
    # Club CMSes carry no feed/sitemap dates; the page's own publish-time
    # meta must fill published_at (observed 2026-10-18: NULL for 100% of
    # club articles, flooding editions with months-old backlog).
    listing_html = '<a href="https://club3.example/news/sieg-gegen-bonn-89-76">A</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/news":
            return httpx.Response(200, text=listing_html)
        return httpx.Response(200, text=pages.article_html(date_meta="2026-08-05"))

    src3 = Source(id="club3", publisher="Club3", type="club",
                  method="html_listing", url="https://club3.example/news",
                  status="ready")
    con.execute("INSERT INTO sources VALUES ('club3', 'Club3', 'club', NULL, 1)")
    con.execute("INSERT INTO source_state (source_id) VALUES ('club3')")
    f = Fetcher(user_agent=UA, min_host_interval_s=0.0,
                transport=httpx.MockTransport(handler))
    collect_source(con, src3, f, tmp_path, now=NOW)
    published, stale = con.execute(
        "SELECT published_at, stale FROM articles WHERE source_id = 'club3'"
    ).fetchone()
    assert published is not None and published.date().isoformat() == "2026-08-05"
    assert stale is False


def test_undated_article_is_stored_stale(con, registry, tmp_path):
    # The shared fixture's article pages carry no date signal at all, and
    # html_listing discovery supplies none either: age unknown -> stale.
    # Feed items (pubDate) stay fresh.
    _sync(con, registry)
    f = make_fetcher()
    collect_source(con, registry.by_id("bbl_newscenter"), f, tmp_path, now=NOW)
    collect_source(con, registry.by_id("club"), f, tmp_path, now=NOW)
    rows = dict(con.execute(
        "SELECT source_id, bool_and(stale) FROM articles GROUP BY source_id"
    ).fetchall())
    assert rows["bbl_newscenter"] is True
    assert rows["club"] is False
    assert con.execute(
        "SELECT count(*) FROM articles WHERE source_id = 'club' AND published_at IS NULL"
    ).fetchone()[0] == 0


def test_from_html_extracts_publish_date():
    from bbl_newsletter.extract import from_html

    dated = from_html(pages.article_html(date_meta="2026-10-11"),
                      url="https://club.example/news/sieg")
    assert dated.published_at is not None
    assert dated.published_at.date().isoformat() == "2026-10-11"
    undated = from_html(pages.article_html(), url="https://club.example/news/sieg")
    assert undated.published_at is None


def test_implausible_page_dates_rejected():
    # The body-text date fallback latched onto player BIRTH dates on
    # rasta-vechta.de signing articles (observed live 2026-10-18:
    # published_at 1996-05-02 on a 2026 announcement). A date outside the
    # plausibility bounds must yield no date (-> stale), and a plausible
    # visible date must still win over the implausible one.
    from datetime import datetime
    from bbl_newsletter.extract import from_html

    body = ("<p>" + "Rasta Vechta hat einen neuen Spieler verpflichtet und "
            "freut sich auf die kommende Saison im RASTA Dome. " * 8 +
            "Geboren am 2. Mai 1996 in Lansing, Michigan.</p>")
    page = ("<!doctype html><html lang=\"de\"><head><title>Rasta holt Trice</title>"
            "</head><body><main><article><h1>Rasta holt Trice</h1>"
            f"{body}</article></main></body></html>")
    now = datetime(2026, 10, 18)
    assert from_html(page, url="https://www.rasta-vechta.de/news/x", now=now).published_at is None
    dated = page.replace("</h1>", '</h1><span class="date">02.08.2026</span>')
    got = from_html(dated, url="https://www.rasta-vechta.de/news/x", now=now).published_at
    assert got is not None and got.date().isoformat() == "2026-08-02"
