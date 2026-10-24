import json
from pathlib import Path

import httpx
import pytest

from bbl_newsletter import db
from bbl_newsletter.fetch import Fetcher
from bbl_newsletter.schedule import (
    collect_schedule, game_ids_from_listing, parse_game_page, resolve_team,
    upsert_game,
)

SCHEMA = Path(__file__).parents[1] / "schema.sql"
UA = "bbl-newsletter-collector/0.1 (test)"


def game_obj(gid="2006923", status="SCHEDULED", home_score=None, guest_score=None,
             home=("10", "FC Bayern München Basketball"),
             guest=("20", "Science City Jena"), when="2026-10-12T18:30:00Z"):
    return {
        "id": int(gid),
        "seasonId": 2026,
        "competition": "BBL",
        "status": status,
        "scheduledTime": when,
        "matchDay": 5,
        "homeTeam": {"teamId": home[0], "seasonTeamId": "1" + home[0], "name": home[1]},
        "guestTeam": {"teamId": guest[0], "seasonTeamId": "1" + guest[0], "name": guest[1]},
        "result": {"homeTeamFinalScore": home_score,
                   "guestTeamFinalScore": guest_score},
    }


def game_page(gid="2006923", status="SCHEDULED", home_score=None, guest_score=None,
              recent=None):
    data = {
        "props": {
            "pageProps": {
                "initialGameData": game_obj(gid, status, home_score, guest_score),
                "initialRecentGames": recent or {},
            }
        }
    }
    return (
        "<html><head><script id=\"__NEXT_DATA__\" type=\"application/json\">"
        + json.dumps(data)
        + "</script></head><body></body></html>"
    )


LISTING = """<html><body>
<a href="/spiele/2006923">Bayern - Jena</a>
<a href="/spiele/2006924?ref=x&amp;page=1">Alba - Bonn</a>
<a href="/spiele/2006923">Bayern - Jena nochmal</a>
<a href="/tabelle">Tabelle</a>
</body></html>"""


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_schema(c, SCHEMA)
    yield c
    c.close()


def test_parse_scheduled_game():
    rec = parse_game_page(game_page())
    assert rec.uid == "bbl:2006923"
    assert rec.season_uid == "bbl-2026-27"
    assert rec.status == "scheduled"
    assert rec.matchday == 5
    assert rec.home_score is None and rec.guest_score is None
    assert rec.scheduled_time.hour == 18  # UTC kept


def test_parse_final_game_scores():
    rec = parse_game_page(game_page(status="OFFICIAL", home_score=89, guest_score=76))
    assert rec.status == "final"
    assert (rec.home_score, rec.guest_score) == (89, 76)


def test_parse_id_mismatch_raises():
    with pytest.raises(ValueError, match="mismatch"):
        parse_game_page(game_page(gid="2006923"), expected_id="999")


def test_listing_extraction_dedupes():
    assert game_ids_from_listing(LISTING) == ["2006923", "2006924"]


def test_team_resolution_uses_canonical_slugs(con):
    slug = resolve_team(con, "10", "FC Bayern München Basketball")
    assert slug == "bayern"
    # idempotent, no second row
    assert resolve_team(con, "10", "FC Bayern München Basketball") == "bayern"
    rows = con.execute("SELECT count(*) FROM teams").fetchone()[0]
    assert rows == 1
    aliases = con.execute(
        "SELECT aliases FROM teams WHERE team_id = 'bayern'").fetchone()[0]
    assert "FC Bayern München Basketball" in aliases


def test_upsert_then_finalize(con):
    upsert_game(con, parse_game_page(game_page()))
    upsert_game(con, parse_game_page(
        game_page(status="OFFICIAL", home_score=89, guest_score=76)))
    row = con.execute(
        "SELECT status, home_score, away_score, matchday FROM games "
        "WHERE game_uid = 'bbl:2006923'").fetchone()
    assert row == ("final", 89, 76, 5)
    n = con.execute("SELECT count(*) FROM games").fetchone()[0]
    assert n == 1


def test_collect_schedule_walks_embedded_games(con):
    # listing seeds only 2006923/2006924; 2006925 arrives embedded in a page
    state = {"2006923": ("SCHEDULED", None, None),
             "2006924": ("OFFICIAL", 101, 95),
             "2006925": ("SCHEDULED", None, None)}
    fetched_pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/spielplan":
            return httpx.Response(200, text=LISTING)
        gid = path.rsplit("/", 1)[-1]
        fetched_pages.append(gid)
        status, hs, gs = state[gid]
        recent = {
            "past": [game_obj("2006924", "OFFICIAL", 101, 95,
                              home=("30", "Alba Berlin"),
                              guest=("40", "Telekom Baskets Bonn"))],
            "future": [game_obj("2006925", "SCHEDULED",
                                home=("50", "ratiopharm Ulm"),
                                guest=("60", "Rasta Vechta"),
                                when="2026-10-14T18:30:00Z")],
        }
        return httpx.Response(200, text=game_page(gid, status, hs, gs, recent=recent))

    f = Fetcher(user_agent=UA, min_host_interval_s=0.0,
                transport=httpx.MockTransport(handler))
    url = "https://www.easycredit-bbl.de/spielplan"
    rep = collect_schedule(con, f, url)
    assert rep["ids_seen"] == 2 and not rep["errors"]
    # all three games present although only non-final pages were fetched
    uids = {r[0] for r in con.execute("SELECT game_uid FROM games").fetchall()}
    assert uids == {"bbl:2006923", "bbl:2006924", "bbl:2006925"}
    assert "2006924" not in fetched_pages  # final: upserted from embeds only

    # second run: 2006923 finished meanwhile; its neighbor walk updates it
    state["2006923"] = ("OFFICIAL", 89, 76)
    fetched_pages.clear()
    f2 = Fetcher(user_agent=UA, min_host_interval_s=0.0,
                 transport=httpx.MockTransport(handler))
    rep2 = collect_schedule(con, f2, url)
    assert "2006924" not in fetched_pages
    status = con.execute(
        "SELECT status FROM games WHERE game_uid = 'bbl:2006923'").fetchone()[0]
    assert status == "final"


def test_final_seed_still_anchors_discovery(con):
    # the listing's only seed is already final in the DB; the run must still
    # fetch that one page and discover the future neighbor from its embeds
    from bbl_newsletter.schedule import upsert_game, record_from_game
    upsert_game(con, record_from_game(game_obj("2006923", "OFFICIAL", 89, 76)))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/spielplan":
            return httpx.Response(200, text='<a href="/spiele/2006923">x</a>')
        recent = {"future": [game_obj("2006925", "SCHEDULED",
                                      home=("50", "ratiopharm Ulm"),
                                      guest=("60", "Rasta Vechta"),
                                      when="2026-10-14T18:30:00Z")]}
        return httpx.Response(
            200, text=game_page("2006923", "OFFICIAL", 89, 76, recent=recent))

    f = Fetcher(user_agent=UA, min_host_interval_s=0.0,
                transport=httpx.MockTransport(handler))
    rep = collect_schedule(con, f, "https://www.easycredit-bbl.de/spielplan")
    assert rep["pages_fetched"] >= 1
    uids = {r[0] for r in con.execute("SELECT game_uid FROM games").fetchall()}
    assert "bbl:2006925" in uids


# ------------------------------------------------------------- games API
API_FIXTURE = Path(__file__).parent / "fixtures" / "games_api_page1_2026-10-19.json"
LISTING_PROPS = Path(__file__).parent / "fixtures" / "schedule_listing_next_data_2026-10-19.json"


def listing_with_key(key="k" * 64):
    return (
        "<html><head><script id=\"__NEXT_DATA__\" type=\"application/json\">"
        + json.dumps({"props": {"pageProps": {"key": key}}})
        + "</script></head><body></body></html>"
    )


def api_fetcher(pages, seen):
    """MockTransport: serves the listing (for the key) and API pages."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "api.basketball-bundesliga.de":
            seen.append(dict(request.headers) | {"_q": str(request.url.query)})
            page = int(request.url.params["currentPage"])
            return httpx.Response(200, json=pages[page - 1])
        return httpx.Response(200, text=listing_with_key())
    return Fetcher(user_agent=UA, min_host_interval_s=0.0,
                   transport=httpx.MockTransport(handler))


def test_api_collects_cup_games_from_real_capture(con):
    # real page-1 capture 2026-10-19: 100 items, BBL and BBL_CUP mixed
    from bbl_newsletter.schedule import collect_schedule_api

    page1 = json.loads(API_FIXTURE.read_text(encoding="utf-8"))
    page1 = dict(page1, totalPages=2)
    page2 = {"currentPage": 2, "totalPages": 2, "pageSize": 100, "items": []}
    seen = []
    f = api_fetcher([page1, page2], seen)
    rep = collect_schedule_api(con, f, "https://www.easycredit-bbl.de/saison/spielplaene",
                               season_id=2026)
    assert rep["pages_fetched"] == 2 and not rep["errors"]
    assert rep["upserted"] == len(page1["items"])
    kinds = dict(con.execute(
        "SELECT competition, count(*) FROM games g JOIN seasons s USING (season_uid) "
        "GROUP BY 1").fetchall())
    assert kinds.get("pokal", 0) > 0 and kinds.get("bbl", 0) > 0
    # credential sent as the site itself does: reversed page key
    assert seen[0]["x-api-key"] == "publicWebUser"
    assert seen[0]["x-api-secret"] == ("k" * 64)[::-1]


def test_api_official_game_with_null_scores_kept_not_overwritten(con):
    # the API marks games OFFICIAL with null finals (observed 2026-10-19);
    # the record must not raise, and a known score must survive the upsert
    from bbl_newsletter.schedule import collect_schedule_api

    upsert_game(con, parse_game_page(
        game_page("2007036", "OFFICIAL", 102, 75)))
    null_final = game_obj("2007036", "OFFICIAL")
    null_final["competition"] = "BBL"
    page = {"currentPage": 1, "totalPages": 1, "pageSize": 100,
            "items": [null_final]}
    f = api_fetcher([page], [])
    rep = collect_schedule_api(con, f, "https://www.easycredit-bbl.de/saison/spielplaene",
                               season_id=2026)
    assert not rep["errors"] and rep["upserted"] == 1
    row = con.execute(
        "SELECT status, home_score, away_score FROM games "
        "WHERE game_uid = 'bbl:2007036'").fetchone()
    assert row == ("final", 102, 75)


def test_api_tbd_game_skipped_stage_survives(con):
    # the API lists TBD placeholders (next cup round) with scheduledTime null
    # (observed live 2026-10-20: the None raised AttributeError and killed
    # the whole schedule stage); one bad record must skip, the rest upsert
    from bbl_newsletter.schedule import collect_schedule_api

    tbd = game_obj("2007050", when=None)
    tbd["isTBD"] = True
    good = game_obj("2007051", when="2026-10-25T14:00:00Z",
                    home=("50", "ratiopharm Ulm"), guest=("60", "Rasta Vechta"))
    page = {"currentPage": 1, "totalPages": 1, "pageSize": 100,
            "items": [tbd, good]}
    f = api_fetcher([page], [])
    rep = collect_schedule_api(con, f, "https://www.easycredit-bbl.de/saison/spielplaene",
                               season_id=2026)
    assert rep["upserted"] == 1
    assert len(rep["errors"]) == 1 and "2007050" in rep["errors"][0]
    uids = {r[0] for r in con.execute("SELECT game_uid FROM games").fetchall()}
    assert uids == {"bbl:2007051"}


def test_listing_capture_carries_api_key():
    # the real listing __NEXT_DATA__ capture: pageProps.key is where the
    # credential lives (redacted in the fixture, presence is the contract)
    props = json.loads(LISTING_PROPS.read_text(encoding="utf-8"))
    page_props = props["props"]["pageProps"] if "props" in props else props
    assert "key" in page_props
