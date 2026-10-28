# bbl-newsletter

Daily English BBL newsletter pipeline. Runs on HUB (CM4 wakes it first), stores
locally on HUB, backs up to the NAS, publishes via an `editions/` branch to the
website repo where GitHub Actions validates and deploys.

Configuration and schema live in the repository root:
`newsletter_sources.yaml` (source registry) and `schema.sql` (DuckDB schema).
Migrated out of the Homelab-Vault 2026-10-24; the vault keeps the
operational documentation (handoffs, run notes, registry decision records)
and the website/deploy scaffolds.

## Layout

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
