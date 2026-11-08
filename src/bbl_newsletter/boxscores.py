"""Read-only canonical BBL box scores; no scraping, name matching or model calls."""
from __future__ import annotations

import logging
import math
import sqlite3
from html import escape
from pathlib import Path

LOG = logging.getLogger(__name__)
STATS = ('seconds_played', 'points', 'two_point_made', 'two_point_attempted',
         'three_point_made', 'three_point_attempted', 'field_goals_made',
         'field_goals_attempted', 'free_throws_made', 'free_throws_attempted',
         'offensive_rebounds', 'defensive_rebounds', 'total_rebounds', 'assists',
         'steals', 'turnovers', 'blocks', 'fouls_committed', 'plus_minus')


def load_boxscores(con, source: Path, game_uids: list[str]) -> dict[str, dict]:
    """Read one consistent transaction; omit unknown, incomplete or conflicting games.

    canonical_games/shared_franchises are the existing newsletter alignment.
    SQLite repository team/player keys are never confused with provider IDs.
    The configured source is opened mode=ro and is never migrated or modified.
    """
    output = {}
    try:
        db = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    except sqlite3.Error as exc:
        LOG.warning('Box-score source unavailable: %s', exc)
        return output
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        for uid in sorted(set(game_uids)):
            prefix, _, provider_id = uid.partition(':')
            if prefix not in ('bbl', 'pokal') or not provider_id.isdigit():
                continue
            try:
                mapping = con.execute('''SELECT game_id, home_team_id, away_team_id,
                    home_franchise_id, away_franchise_id FROM canonical_games
                    WHERE game_uid=?''', [uid]).fetchone()
                if not mapping or mapping[0] != int(provider_id):
                    raise ValueError('game missing from canonical reference')
                raw = db.execute('SELECT * FROM games WHERE game_id=?', [mapping[0]]).fetchone()
                if raw is None or raw['status'] != 'OFFICIAL':
                    raise ValueError('official result not available')
                expected_competition = 'BBL' if prefix == 'bbl' else 'BBL_CUP'
                if raw['competition'] != expected_competition:
                    raise ValueError('competition identity conflict')
                actual = (raw['home_team_id'], raw['away_team_id'],
                          raw['home_franchise_id'], raw['guest_franchise_id'])
                if actual != tuple(mapping[1:]) or None in actual:
                    raise ValueError('canonical team identity conflict')
                teams = [dict(r) for r in db.execute('SELECT * FROM game_team_stats WHERE game_id=?', [mapping[0]])]
                players = [dict(r) for r in db.execute('SELECT * FROM game_player_stats WHERE game_id=?', [mapping[0]])]
                # The canonical export normalizes legacy empty numeric fields to NULL.
                for record in teams + players:
                    for field in STATS:
                        if record[field] == '':
                            record[field] = None
                        value = record[field]
                        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                            raise ValueError('invalid numeric statistic')
                scores = (raw['home_final_score'], raw['guest_final_score'])
                if len(teams) != 2 or any(s is None or s < 0 for s in scores):
                    raise ValueError('incomplete team totals')
                sides = []
                seen_players = set()
                for index, (side, source_side) in enumerate((('home', 'home'), ('away', 'guest'))):
                    tid, fid = actual[index], actual[index + 2]
                    team = [t for t in teams if t['team_id'] == tid and t['franchise_id'] == fid]
                    roster = [p for p in players if p['team_id'] == tid and p['franchise_id'] == fid]
                    if len(team) != 1 or team[0]['home_away'] != side or not roster:
                        raise ValueError('incomplete side or player identity conflict')
                    if team[0]['points'] != scores[index] or any(p['points'] is None for p in roster):
                        raise ValueError('missing or inconsistent points')
                    if sum(p['points'] for p in roster) != scores[index]:
                        raise ValueError('player points do not reconcile to final score')
                    for player in roster:
                        identity = player['bbl_season_player_id']
                        if not player['player_id'] or identity in seen_players:
                            raise ValueError('missing or duplicate player identity')
                        seen_players.add(identity)
                        for field in STATS:
                            value = player[field]
                            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                                raise ValueError('invalid numeric statistic')
                    quarters = [raw[f'{source_side}_q{n}_score'] for n in range(1, 5)]
                    overtime = raw[f'{source_side}_overtime_score']
                    if all(q is not None for q in quarters) and (not raw['overtime_periods'] or overtime is not None):
                        if sum(quarters) + (overtime or 0) != scores[index]:
                            raise ValueError('quarter scores do not reconcile')
                    sides.append({'name': raw[f'{source_side}_team_name'], 'score': scores[index],
                                  'quarters': quarters, 'overtime': overtime,
                                  'stats': {k: team[0][k] for k in STATS},
                                  'players': [{'player_id': p['player_id'], 'name': p['player_name'],
                                               **{k: p[k] for k in STATS}} for p in roster]})
                if len(players) != sum(len(s['players']) for s in sides):
                    raise ValueError('unmatched player rows')
                output[uid] = {'game_id': mapping[0], 'game_uid': uid,
                               'overtime_periods': raw['overtime_periods'], 'sides': sides}
            except Exception as exc:
                LOG.warning('Box score unavailable for %s: %s', uid, exc)
    finally:
        db.close()
    return output


def number(value) -> str:
    return '—' if value is None else f'{value:g}'


def _shots(row, prefix):
    return f'{number(row[prefix + "_made"])}/{number(row[prefix + "_attempted"])}'


def _table(headers, rows, label):
    head = ''.join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(
        f'<th scope="row">{escape(str(cell))}</th>' if i == 0 else f'<td>{escape(str(cell))}</td>'
        for i, cell in enumerate(row)) + '</tr>' for row in rows)
    return (f'<div class="bbl-boxscore-scroll" role="region" aria-label="{escape(label)}" tabindex="0">'
            f'<table class="bbl-boxscore-table" aria-label="{escape(label)}"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def render_boxscore(box: dict) -> str:
    sides = box['sides']
    title = f"{sides[0]['name']} {number(sides[0]['score'])}–{number(sides[1]['score'])} {sides[1]['name']}"
    lines = ['<div class="bbl-boxscore">', '<p class="bbl-boxscore-label">Final score</p>',
             f'<p class="bbl-boxscore-result"><strong>{escape(title)}</strong></p>']
    headers = ['Team', 'Q1', 'Q2', 'Q3', 'Q4']
    if box['overtime_periods']:
        headers.append('OT' if box['overtime_periods'] == 1 else 'OT total')
    headers.append('Final')
    lines.append(_table(headers, [[s['name'], *map(number, s['quarters']),
                  *([number(s['overtime'])] if box['overtime_periods'] else []),
                  number(s['score'])] for s in sides], 'Quarter scores'))
    lines.append('<p><strong>Team statistics</strong></p>')
    lines.append(_table(['Team', 'FG', '3P', 'FT', 'REB', 'AST', 'STL', 'BLK', 'TO', 'PF'],
        [[s['name'], _shots(s['stats'], 'field_goals'), _shots(s['stats'], 'three_point'),
          _shots(s['stats'], 'free_throws'), *[number(s['stats'][k]) for k in
          ('total_rebounds', 'assists', 'steals', 'blocks', 'turnovers', 'fouls_committed')]] for s in sides],
        'Team statistics'))
    lines += ['<details class="bbl-boxscore-players"><summary>Player box scores — both teams</summary>']
    for side in sides:
        lines.append(f'<p><strong>{escape(side["name"])}</strong></p>')
        rows = []
        for p in side['players']:
            seconds = p['seconds_played']
            minutes = '—' if seconds is None else f'{int(seconds) // 60}:{int(seconds) % 60:02d}'
            rows.append([p['name'], minutes, number(p['points']), _shots(p, 'two_point'),
                         _shots(p, 'three_point'), _shots(p, 'free_throws'),
                         *[number(p[k]) for k in ('offensive_rebounds', 'defensive_rebounds',
                          'total_rebounds', 'assists', 'steals', 'blocks', 'turnovers', 'fouls_committed', 'plus_minus')]])
        lines.append(_table(['Player', 'MIN', 'PTS', '2P', '3P', 'FT', 'OREB', 'DREB', 'REB',
                             'AST', 'STL', 'BLK', 'TO', 'PF', '+/−'], rows, side['name'] + ' player statistics'))
    lines += ['</details>', '</div>']
    return '\n'.join(lines)
