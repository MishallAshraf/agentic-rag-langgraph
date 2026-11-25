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
