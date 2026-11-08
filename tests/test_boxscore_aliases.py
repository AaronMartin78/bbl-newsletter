from datetime import datetime
from pathlib import Path

from bbl_newsletter import db
from bbl_newsletter.cluster import _team_patterns


def test_recap_club_names_work_without_sponsors_or_bare_city_matching(tmp_path):
    c=db.connect(tmp_path/'aliases.duckdb')
    db.init_schema(c,Path(__file__).parents[1]/'schema.sql')
    c.execute("INSERT INTO teams VALUES ('trier','414','VET-CONCEPT Gladiators Trier',NULL,[]),('bamberg','420','BMA365 Bamberg Baskets',NULL,[])")
    patterns=_team_patterns(c)
    title='Souveräner Sieg der Gladiators Trier gegen Bamberg Baskets'
    assert all(p.search(title) for p in patterns.values())
    assert not patterns['trier'].search('Stadt Trier')
    assert not patterns['bamberg'].search('Stadt Bamberg')
    c.close()
