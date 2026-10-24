#!/usr/bin/env bash
# Quality-gate run: clear fixable rejects (parser/validator bugs now fixed),
# re-cluster (generic-token fix changes tagging), summarize, render, show.
# Safe to re-run; completed summaries are skipped.
set -o pipefail
export PATH="$HOME/.local/bin:$PATH"   # uv; non-interactive ssh skips .bashrc
cd "$(dirname "$0")"
cp config.example.yaml config.yaml
echo "== cleanup (listing junk + stale team tags) =="
uv run python -m bbl_newsletter --config config.yaml cleanup 2>&1
echo "== clear fixable rejects =="
uv run python -c "
import duckdb, json
con = duckdb.connect('data/newsletter.duckdb')
rows = con.execute(\"select summary_id, validation from summaries where status='rejected'\").fetchall()
drop = [r[0] for r in rows
        if set(json.loads(r[1]).get('failed') or []) <= {'parse', 'numbers_traceable'}]
for sid in drop:
    con.execute('delete from edition_items where summary_id = ?', [sid])
    con.execute('delete from summaries where summary_id = ?', [sid])
print(f'cleared {len(drop)}')
"
echo "== cluster =="
uv run python -m bbl_newsletter --config config.yaml cluster 2>&1
echo "== summarize (expect a few minutes per story) =="
uv run python -m bbl_newsletter --config config.yaml summarize --limit 12 2>&1
echo "== render =="
uv run python -m bbl_newsletter --config config.yaml --date "$(date +%F)" render 2>&1
echo "== edition =="
cat "site/bbl/$(date +%F).qmd" 2>&1
