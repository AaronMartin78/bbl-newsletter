# bbl-newsletter

Daily English BBL newsletter pipeline. Runs on HUB (CM4 wakes it first), stores
locally on HUB, backs up to the NAS, publishes via an `editions/` branch to the
website repo where GitHub Actions validates and deploys.

Configuration and schema live in the repository root:
`newsletter_sources.yaml` (source registry) and `schema.sql` (DuckDB schema).
Migrated out of the Homelab-Vault 2026-10-24; the vault keeps the
operational documentation (handoffs, run notes, registry decision records)
and the website/deploy scaffolds.

## Game box scores

Set `boxscore_database` to the existing canonical BBL SQLite database (on HUB:
`/mnt/nas_docs/BBL/BBL/data/processed/bbl.db`). The renderer opens one read-only
transaction and joins through the newsletter's existing `canonical_games`
alignment. It never scrapes, allocates IDs or changes the source database.
Official BBL/Pokal recaps get final and quarter scores, team statistics and
expandable player tables, with an official game-page citation. An explicit,
unambiguous `article_games` recap link also supports an older general-news
story whose own `game_uid` is absent. Sponsor-free club aliases for Bamberg
Baskets and Gladiators Trier use the existing tagging rules.

Team identities, competition, duplicate players, quarter totals and player
points must reconcile before a box score is shown. Missing numeric values
remain unknown, including legacy empty numeric fields. Previews and unrelated
news get no score card. An unavailable source or incomplete record produces
“Box score unavailable” on eligible recaps and does not block publication.

To test a published edition, copy `data/newsletter.duckdb` while no writer is
active, run the renderer against that copy and the read-only source, then run
`tools/check_edition.py` and the website's Quarto build. This leaves published
summary text, summary IDs and live edition receipts untouched until deployment.

## Layout

- `src/bbl_newsletter/league.py` / `league_db.py` - league profiles and the
  league-database loader; see "Other leagues".
- `src/bbl_newsletter/ids.py` - hash/id conventions (canonical URL, article_id,
  content_hash, story_id, summary_id, game_uid). The single implementation of
  what schema.sql documents.
- `src/bbl_newsletter/registry.py` - loads and validates the source registry;
  partitions sources into usable / pending_check / disabled.
- `src/bbl_newsletter/db.py` - DuckDB connection, schema init, registry sync.
  Single writer: only the pipeline process on HUB.
- `src/bbl_newsletter/fetch.py` - polite fetch layer: robots.txt per host
  (unreachable robots = deny this run), conditional GET, size cap, per-host
  rate limit. Transport injectable, so tests never touch the network.
- `src/bbl_newsletter/discover.py` - entry point -> bounded item list per
  method: rss (feedparser, full-body feeds reused), news sitemap (urlset or
  one-level sitemapindex, recency window), html listing (link pattern).
- `src/bbl_newsletter/relevance.py` - basketball keyword filter for
  mixed-sport regional sources; club/league/aggregator sources bypass it.
  Errs toward false positives; team tagging proper happens later.
- `src/bbl_newsletter/extract.py` - body extraction via trafilatura; feed
  bodies accepted when substantial (>= 300 chars).
- `src/bbl_newsletter/collect.py` - the collect stage: discovery -> filter ->
  fetch -> store. Articles keyed (article_id, content_hash); body text under
  data_dir/articles/. Runs recorded in runs/run_sources. v1 limitation:
  a known article_id is not re-fetched, so later revisions of an already
  collected article are not yet captured.

- `src/bbl_newsletter/schedule.py` - schedule loader: scans a schedule page
  for /spiele/<id> links, parses each game page's __NEXT_DATA__
  (initialGameData; field contract from the reviewed Hermes collector, to be
  verified once against a live page from the LAN), upserts seasons/teams/
  games. Teams are auto-created from official ids with canonical slugs;
  final games are never re-fetched.

- `src/bbl_newsletter/cluster.py` - deterministic clustering, no embeddings:
  team alias tagging -> game attachment (both teams tagged, publication
  within 36h of tipoff, nearest tipoff wins, exact tie attaches nothing) ->
  stories. Game stories carry every attached article including older
  previews; non-game articles sharing team + day merge into one story when
  word-shingle Jaccard >= 0.5 (syndicated copies), otherwise stay separate;
  all-European-cup components become kind='euroleague'. Story ids are
  deterministic, so re-clustering never duplicates.

- `src/bbl_newsletter/summarize.py` - summarization with a validation gate.
  The LLM is an injectable callable (Ollama on HUB plugs in once
  benchmarked); prompt v1 demands a composite English synthesis as JSON
  (headline + summary), never URLs. The validator checks numbers are
  traceable to source bodies, output is English, length is sane; failures
  are stored as 'rejected' with their validation record and never rendered.
  Summary ids are deterministic, replays are idempotent.
- `src/bbl_newsletter/render.py` - deterministic Quarto renderer: one
  <date>.qmd per edition, sections Games / EuroLeague / Around the League,
  every citation link taken from the DB (story members), never from model
  output. Records editions/edition_items; re-rendering is byte-identical.
  No publishable summaries = no edition file.

- `src/bbl_newsletter/__main__.py` - runner CLI. `python -m bbl_newsletter
  --config config.yaml run [--date YYYY-MM-DD]` executes collect ->
  schedule -> cluster -> summarize -> render -> publish; each stage also
  runs alone (`--model` overrides the config model; `--limit` caps model
  calls; `cleanup` removes wrongly stored listing-page articles).
  `config.example.yaml` is the template; copy to config.yaml on HUB.
- `src/bbl_newsletter/publish.py` - pushes the day's edition into the
  website repo's `editions` branch (local clone on HUB, `website_repo` in
  config); records status and commit hash in editions. Identical content
  re-publishes as a no-op.
- `src/bbl_newsletter/maintenance.py` - DB cleanup for the 2026-10-13
  listing-page collection bug: removes junk articles with their stories,
  summaries and edition items so re-clustering rebuilds clean.
- `run_daily.sh` - the full daily run on HUB plus NAS backup and Uptime
  Kuma heartbeat; triggered by CM4 (see ../deploy/README.md).

Related: `../website/` (Quarto site + CI workflows; its CI validator is
cross-tested against this renderer's output), `../deploy/` (CM4 wake
timer units and install notes).

Not yet built: private delivery of full translations (Discord webhook).

## Other leagues

The same package writes one newsletter per league. Each league has its own
`config_<league>.yaml` (template: `config.league.example.yaml`), DuckDB,
data folder and source registry; `config.yaml` without a `league` block is
the BBL and behaves exactly as before. Run a league with
`python -m bbl_newsletter --config config_acb.yaml run`.

`src/bbl_newsletter/league.py` holds the profile (name, source language,
competition codes, official game URL, player-id column, edition title) and
the per-language words for the English check, round names and promo
filter. For `games_source: league_db` leagues, `league_db.py` loads the
latest season's teams and games read-only from the league database that the
mule collector publishes to the NAS lake (`boxscore_database` may name the
lake's `current.json`), so the schedule stage scrapes nothing:

- `game_uid` = `<prefix>:<games.game_id>`, the league database's game id;
- `team_id` = `<league>:<franchise_id>`, the provider's stable club id
  (also in `teams.bbl_id`); sponsor names go to `team_aliases`.

Box scores come from the same file with the same reconciliation checks.

| League | code | language | competitions | player_identity_column | official_game_url |
|---|---|---|---|---|---|
| Liga Endesa | acb | es | `{acb: ACB}` | acb_player_id | `https://live.acb.com/es/partidos/{game_id}/estadisticas` |
| LNB Elite | lnb | fr | `{lnb: LNB}` | lnb_player_id | empty for now |
| Greek Basket League | gbl | el | `{gbl: GBL}` | esake_player_id | `https://www.esake.gr/el/action/EsakegameView?idgame={game_id:08X}` |
| ABA League | aba | sh | `{aba: ABA}` | aba_player_id | empty (`game_id` is season x 10000 + match) |

Editions go to `<code>/posts/` in the website repo.

## Run the pipeline

    cp config.example.yaml config.yaml   # set ollama.model after benchmark
    uv run python -m bbl_newsletter --config config.yaml run

## Link to shared BBL IDs

    uv run python -m bbl_newsletter --config config.yaml link --reference DIR

`DIR` is a shared reference package published from the NAS database by the
vault's `Databases/bbl_shared_keys.py`. `link` verifies its checksums, loads
it into `shared_*` tables, and reports BBL/Pokal games and teams that do not
resolve plus any game whose teams disagree with the reference (exit 1 on a
conflict). Joins go through `canonical_games` and `canonical_article_teams`;
the pipeline never mints canonical IDs.

## Run tests

    uv run pytest

All tests run offline against fixtures; `test_db.py` also validates the real
registry and schema from `Newsletter/`.
