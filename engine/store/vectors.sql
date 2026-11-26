-- Document chunks and their embeddings (Postgres + pgvector, Lampus Phase 1, Step 5).
-- Applied by engine/vectorstore.py on first use; every statement is safe to run again.
--
-- One table for every bot. The collection column says which bot a chunk belongs
-- to, and every search filters on it, so a bot only ever sees its own documents.

-- Installed in public (on every default search_path), never in a throwaway test
-- schema, where dropping the schema would take the extension with it.
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

CREATE TABLE IF NOT EXISTS chunks (
    collection TEXT NOT NULL,           -- the bot's collection, e.g. bot_customer_handbook
    id         TEXT NOT NULL,           -- stable chunk id from ingestion (file:page:section:offset)
    content    TEXT NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{}',
    embedding  vector(384) NOT NULL,    -- BAAI/bge-small-en-v1.5
    PRIMARY KEY (collection, id)        -- also the index that finds one bot's chunks
);

-- No approximate (HNSW) index, on purpose (course topic 20, loadtest/bench_hnsw.py).
-- Every search is inside one bot, and a bot holds a few hundred to a few thousand
-- chunks: Postgres finds them with the primary key and compares each one, exact,
-- in about 2 ms. With an HNSW index on a table shared by 50 bots, the planner chose
-- the index for these searches, walked the 40 nearest chunks of ALL bots, then
-- threw away other bots' rows: 0.7 of 10 results came back. Add one when a single
-- bot outgrows exact search (measured: 42 ms at 50,000 chunks), together with
-- pgvector 0.8's iterative scan (SET hnsw.iterative_scan = strict_order), which
-- keeps walking the graph until enough rows pass the filter.

-- Data lineage (course topic 22, Lampus Phase 1, Step 6): for every chunk, which
-- file it came from (and that file's exact content, by hash), and which ingestion
-- run made it, with that run's settings and code version. Also lets ingestion skip
-- files that haven't changed (engine/lineage.py).

CREATE TABLE IF NOT EXISTS ingest_runs (
    id              BIGSERIAL PRIMARY KEY,
    collection      TEXT NOT NULL,
    folder          TEXT NOT NULL,          -- documents folder, relative to the project root
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,            -- NULL: the run failed or is still going
    git_commit      TEXT,                   -- code version; "+changes" when the working tree had edits
    settings        JSONB NOT NULL,         -- embedding model, chunk size and overlap, loader, pipeline version
    files_seen      INT,
    files_changed   INT,                    -- new or edited files, re-chunked
    files_removed   INT,
    chunks_embedded INT,                    -- chunks sent to the embedding model (the expensive part)
    chunks_reused   INT                     -- chunks whose text was unchanged, so their old vector was kept
);

CREATE TABLE IF NOT EXISTS source_files (
    collection    TEXT NOT NULL,
    path          TEXT NOT NULL,            -- relative to the run's folder
    sha256        TEXT NOT NULL,            -- of the file's bytes: any edit changes it
    size_bytes    BIGINT NOT NULL,
    settings_hash TEXT NOT NULL,            -- settings it was chunked with; new settings mean re-chunk
    ingest_run    BIGINT NOT NULL REFERENCES ingest_runs (id),
    PRIMARY KEY (collection, path)
);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source_path TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS ingest_run BIGINT REFERENCES ingest_runs (id);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks (collection, source_path);
