-- BBL Newsletter - DuckDB schema, v1 (2026-10-12)
-- Single writer: the pipeline on HUB. DB, extracted article text and
-- rendered editions all live on HUB local disk (owner: plenty of space);
-- the NAS receives a backup copy after each successful run, so a run
-- never depends on NAS availability.
-- Boundary: this schema is independent of Hermes. Teams/games use the
-- official BBL/competition id spaces but are populated by this pipeline's
-- own collection, never read from or written to Hermes trees.
--
-- Hash conventions (sha256, hex):
--   article_id   = sha256(canonical_url)   url normalization: https, lowercase
--                  host, strip tracking params and fragments, no trailing slash
--   content_hash = sha256(extracted_body_text)  new hash = new revision row,
--                  old revisions are kept, never overwritten
--   input_hash   = sha256(concat of sorted member content_hashes)
--   summary_id   = sha256(story_id || input_hash || model || prompt_version)
--                  -> same inputs + same model + same prompt = same id,
--                  so replays cannot create duplicate summaries

-- ---- reference: sources (mirror of newsletter_sources.yaml for joins) ----
CREATE TABLE IF NOT EXISTS sources (
  source_id        TEXT PRIMARY KEY,   -- ids from newsletter_sources.yaml
  publisher        TEXT NOT NULL,
  type             TEXT NOT NULL CHECK (type IN
                     ('league','club','regional_public','regional_commercial','aggregator')),
  url              TEXT,
  registry_version INTEGER NOT NULL    -- sources.yaml version this row was loaded from
);

CREATE TABLE IF NOT EXISTS source_state (        -- conditional-GET + collection state
  source_id     TEXT PRIMARY KEY REFERENCES sources(source_id),
  etag          TEXT,
  last_modified TEXT,
  last_run_at   TIMESTAMP,
  last_status   TEXT                   -- ok | http_<code> | timeout | robots_denied | parse_error
);

-- ---- reference: teams, seasons, games ----
CREATE TABLE IF NOT EXISTS teams (
  team_id  TEXT PRIMARY KEY,           -- stable slug: 'bayern', 'alba', 'jena', ...
  bbl_id   TEXT,                       -- official BBL identifier where known
  name     TEXT NOT NULL,
  city     TEXT,
  aliases  TEXT[]                      -- name variants for article matching (never join on names)
);

CREATE TABLE IF NOT EXISTS seasons (
  season_uid  TEXT PRIMARY KEY,        -- '<competition>-<label>', e.g. 'bbl-2026-27'
  competition TEXT NOT NULL CHECK (competition IN
                ('bbl','pokal','euroleague','eurocup','bcl','fiba_ec','friendly')),
  label       TEXT NOT NULL,           -- '2026-27'
  start_date  DATE,
  end_date    DATE
);

CREATE TABLE IF NOT EXISTS games (
  game_uid     TEXT PRIMARY KEY,       -- '<competition>:<official_game_id>' in that competition's id space
  season_uid   TEXT NOT NULL REFERENCES seasons(season_uid),
  matchday     INTEGER,                -- BBL Spieltag / EL round; NULL for cup rounds
  tipoff       TIMESTAMP,              -- Europe/Berlin
  home_team_id TEXT REFERENCES teams(team_id),
  away_team_id TEXT REFERENCES teams(team_id),
  home_score   INTEGER,
  away_score   INTEGER,
  status       TEXT CHECK (status IN ('scheduled','final','postponed','cancelled'))
);

-- ---- collected articles (append-only; revisions as new rows) ----
CREATE TABLE IF NOT EXISTS articles (
  article_id   TEXT NOT NULL,          -- sha256(canonical_url)
  content_hash TEXT NOT NULL,          -- sha256(extracted body text)
  source_id    TEXT NOT NULL REFERENCES sources(source_id),
  url          TEXT NOT NULL,          -- the canonical URL itself (hash is not reversible)
  title        TEXT,
  published_at TIMESTAMP,              -- as stated by the source
  observed_at  TIMESTAMP NOT NULL,     -- when we fetched it
  language     TEXT DEFAULT 'de',
  body_path    TEXT,                   -- extracted text file on HUB; body not stored in DB
  stale        BOOLEAN DEFAULT FALSE,  -- no extractable publish date: age unknown,
                                       -- never tagged, clustered or rendered
  PRIMARY KEY (article_id, content_hash)
);

-- Migration for databases created before 2026-10-18 (idempotent).
ALTER TABLE articles ADD COLUMN IF NOT EXISTS stale BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS article_teams (       -- revision-independent tagging
  article_id TEXT NOT NULL,
  team_id    TEXT NOT NULL REFERENCES teams(team_id),
  PRIMARY KEY (article_id, team_id)
);

CREATE TABLE IF NOT EXISTS article_exclusions (  -- editorial kill switch:
  article_id TEXT PRIMARY KEY,                   -- tagging and story building
  url        TEXT,                               -- skip these permanently
  reason     TEXT,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS article_games (
  article_id TEXT NOT NULL,
  game_uid   TEXT NOT NULL REFERENCES games(game_uid),
  reference  TEXT CHECK (reference IN ('recap','preview','mention')),
  PRIMARY KEY (article_id, game_uid)
);

-- ---- stories (clusters) and summaries ----
CREATE TABLE IF NOT EXISTS stories (
  story_id   TEXT PRIMARY KEY,         -- '<date>-' || substr(sha256(sorted member article_ids),1,12)
  kind       TEXT NOT NULL CHECK (kind IN ('game','team_news','euroleague','league')),
  game_uid   TEXT REFERENCES games(game_uid),    -- set when kind='game'
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS story_articles (
  story_id   TEXT NOT NULL REFERENCES stories(story_id),
  article_id TEXT NOT NULL,
  PRIMARY KEY (story_id, article_id)
);

CREATE TABLE IF NOT EXISTS summaries (
  summary_id     TEXT PRIMARY KEY,     -- deterministic, see hash conventions
  story_id       TEXT NOT NULL REFERENCES stories(story_id),
  input_hash     TEXT NOT NULL,
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at     TIMESTAMP NOT NULL,
  elapsed_s      DOUBLE,
  text_en        TEXT NOT NULL,
  validation     TEXT,                 -- JSON: per-check pass/fail from the local validator
  status         TEXT NOT NULL CHECK (status IN
                   ('candidate','approved','rejected','published'))
);

-- ---- private full translations (email/Discord to owner, never published) ----
CREATE TABLE IF NOT EXISTS translations (
  article_id   TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  text_en      TEXT NOT NULL,
  model        TEXT NOT NULL,
  created_at   TIMESTAMP NOT NULL,
  delivered_at TIMESTAMP,              -- receipt: NULL until the private send succeeded
  channel      TEXT,
  PRIMARY KEY (article_id, content_hash)
);

-- ---- editions ----
CREATE TABLE IF NOT EXISTS editions (
  edition_date DATE PRIMARY KEY,       -- Europe/Berlin; PK = no duplicate editions by construction
  status       TEXT NOT NULL CHECK (status IN ('draft','pushed','deployed','failed')),
  built_at     TIMESTAMP,
  git_commit   TEXT                    -- commit on editions/ branch; deploy receipt
);

CREATE TABLE IF NOT EXISTS edition_items (
  edition_date DATE NOT NULL REFERENCES editions(edition_date),
  summary_id   TEXT NOT NULL REFERENCES summaries(summary_id),
  section      TEXT NOT NULL CHECK (section IN
                 ('games','team_news','euroleague','links')),
  position     INTEGER NOT NULL,
  PRIMARY KEY (edition_date, summary_id)
);

-- ---- operations log ----
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,        -- uuid
  kind        TEXT NOT NULL CHECK (kind IN ('collect','cluster','summarize','render','deliver')),
  started_at  TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  ok          BOOLEAN,
  detail      TEXT
);

CREATE TABLE IF NOT EXISTS run_sources (         -- per-source result of a collect run
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  source_id   TEXT NOT NULL REFERENCES sources(source_id),
  http_status INTEGER,
  items_seen  INTEGER,
  items_new   INTEGER,
  bytes       BIGINT,
  error       TEXT,
  PRIMARY KEY (run_id, source_id)
);
