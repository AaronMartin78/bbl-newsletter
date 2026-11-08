import hashlib
import sqlite3
from pathlib import Path

import duckdb
import pytest

from bbl_newsletter.boxscores import STATS, load_boxscores, render_boxscore
from bbl_newsletter import db
from bbl_newsletter.cluster import cluster
from bbl_newsletter.render import render_edition
from bbl_newsletter.summarize import summarize_pending
from helpers import EDITION, seed_base, seed_articles
from test_summarize_render import good_generator


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'canonical.sqlite3'
    c = sqlite3.connect(path)
    c.execute('''CREATE TABLE games (game_id INTEGER PRIMARY KEY, competition TEXT,
        status TEXT, home_team_id INTEGER, away_team_id INTEGER, home_franchise_id INTEGER,
        guest_franchise_id INTEGER, home_team_name TEXT, guest_team_name TEXT,
        home_final_score INTEGER, guest_final_score INTEGER, overtime_periods INTEGER,
        home_q1_score INTEGER, home_q2_score INTEGER, home_q3_score INTEGER, home_q4_score INTEGER,
        guest_q1_score INTEGER, guest_q2_score INTEGER, guest_q3_score INTEGER, guest_q4_score INTEGER,
        home_overtime_score INTEGER, guest_overtime_score INTEGER)''')
    c.execute("INSERT INTO games VALUES (1,'BBL','OFFICIAL',101,102,10,30,'Home','Away',89,76,0,20,20,20,29,19,19,19,19,NULL,NULL)")
    for table, identity in [('game_team_stats', 'home_away TEXT'),
                            ('game_player_stats', 'player_id INTEGER,bbl_season_player_id INTEGER,player_name TEXT')]:
        c.execute(f'CREATE TABLE {table} (game_id INTEGER,team_id INTEGER,franchise_id INTEGER,{identity},' + ','.join(k+' REAL' for k in STATS) + ')')
    for tid, fid, side, points in [(101,10,'home',89),(102,30,'away',76)]:
        stats = [None] * len(STATS)
        stats[STATS.index('points')] = points
        stats[STATS.index('seconds_played')] = 2400
        stats[STATS.index('turnovers')] = 0
        vals = [1,tid,fid,side,*stats]
        c.execute('INSERT INTO game_team_stats VALUES ('+','.join('?' for _ in vals)+')', vals)
        vals = [1,tid,fid,tid+1,tid+100,'Player <test>',*stats]
        c.execute('INSERT INTO game_player_stats VALUES ('+','.join('?' for _ in vals)+')', vals)
    c.commit()
    c.close()
    return path


@pytest.fixture
def reference():
    c=duckdb.connect(':memory:')
    add_reference(c)
    yield c
    c.close()


def add_reference(c):
    c.execute('CREATE TABLE canonical_games(game_uid TEXT,game_id INTEGER,home_team_id INTEGER,away_team_id INTEGER,home_franchise_id INTEGER,away_franchise_id INTEGER)')
    c.execute("INSERT INTO canonical_games VALUES ('bbl:1',1,101,102,10,30)")


def change(source, sql):
    with sqlite3.connect(source) as c:
        c.execute(sql)


def test_exact_join_is_read_only_and_unknown_is_not_zero(reference, source):
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    box=load_boxscores(reference,source,['bbl:1'])['bbl:1']
    html=render_boxscore(box)
    assert 'Home 89–76 Away' in html
    assert '40:00' in html
    assert 'Player &lt;test&gt;' in html
    assert '<td>—</td>' in html and '<td>0</td>' in html
    assert '<details' in html and 'both teams' in html
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


@pytest.mark.parametrize('sql', [
    "UPDATE games SET competition='BBL_CUP'",
    'UPDATE games SET home_team_id=999',
    'UPDATE games SET home_franchise_id=999',
    "UPDATE games SET status='PRE'",
    'UPDATE game_team_stats SET points=88 WHERE home_away=\'home\'',
    'UPDATE game_player_stats SET points=1 WHERE team_id=101',
    'UPDATE game_player_stats SET player_id=NULL',
    'UPDATE games SET home_q4_score=28',
    'DELETE FROM game_player_stats WHERE team_id=101',
    'INSERT INTO game_player_stats SELECT * FROM game_player_stats LIMIT 1',
])
def test_conflicting_or_incomplete_source_is_never_attached(reference,source,sql):
    change(source,sql)
    assert load_boxscores(reference,source,['bbl:1'])=={}


def test_competition_namespace_and_missing_reference(reference,source,tmp_path):
    assert load_boxscores(reference,source,['pokal:1','euroleague:1','bbl:2'])=={}
    assert load_boxscores(reference,tmp_path/'absent.sqlite3',['bbl:1'])=={}
    assert not (tmp_path/'absent.sqlite3').exists()


def test_legacy_blank_numeric_and_overtime(reference,source):
    change(source,"UPDATE game_player_stats SET plus_minus=''")
    change(source,'UPDATE games SET overtime_periods=1,home_q4_score=20,home_overtime_score=9,guest_overtime_score=0')
    html=render_boxscore(load_boxscores(reference,source,['bbl:1'])['bbl:1'])
    assert '>OT<' in html and '89–76' in html


def test_real_renderer_adds_only_linked_game_boxscore_and_passes_site_checker(source,tmp_path):
    import subprocess,sys
    c=db.connect(tmp_path/'newsletter.duckdb')
    db.init_schema(c,Path(__file__).parents[1]/'schema.sql')
    seed_base(c)
    seed_articles(c,tmp_path)
    cluster(c,EDITION)
    from datetime import datetime
    summarize_pending(c,good_generator,'test-model:1',now=datetime.combine(EDITION,datetime.min.time()))
    add_reference(c)
    path=render_edition(c,EDITION,tmp_path/'site',boxscore_database=source)
    text=path.read_text(encoding='utf-8')
    assert text.count('class="bbl-boxscore"')==1
    assert text.count('https://www.easycredit-bbl.de/spiele/1')==1
    assert text.index('Home 89–76 Away') < text.index('Sources:')
    checker=Path(__file__).parents[1]/'tools/check_edition.py'
    result=subprocess.run([sys.executable,str(checker),str(path)],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert render_edition(c,EDITION,tmp_path/'site',boxscore_database=source).read_text(encoding='utf-8')==text
    c.close()


@pytest.mark.parametrize('relation,expected', [('recap',1),('preview',0)])
def test_explicit_recaps_resolve_but_previews_do_not(source,tmp_path,relation,expected):
    from datetime import datetime
    from helpers import add_article,RECAP_DE
    c=db.connect(tmp_path/'relations.duckdb')
    db.init_schema(c,Path(__file__).parents[1]/'schema.sql')
    seed_base(c)
    c.execute("UPDATE games SET status='scheduled',home_score=NULL,away_score=NULL")
    aid=add_article(c,tmp_path,'recap','club','A recap',RECAP_DE,datetime(2026,10,12))
    c.execute("INSERT INTO stories VALUES ('saved-recap','team_news',NULL,?)",[datetime(2026,10,12)])
    c.execute("INSERT INTO story_articles VALUES ('saved-recap',?)",[aid])
    c.execute("INSERT INTO article_games VALUES (?,'bbl:1',?)",[aid,relation])
    c.execute("INSERT INTO summaries VALUES ('s','saved-recap','input','test','v3',?,1,'Recap title\n\nEnglish recap text.',NULL,'candidate')",[datetime(2026,10,12)])
    add_reference(c)
    text=render_edition(c,EDITION,tmp_path/'site',boxscore_database=source).read_text(encoding='utf-8')
    assert text.count('class="bbl-boxscore"')==expected
    assert 'Box score unavailable' not in text
    c.close()
