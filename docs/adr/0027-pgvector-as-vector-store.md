# 0027. Document chunks in Postgres with pgvector

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 5
- Supersedes: [0001](0001-chroma-as-vector-store.md)

## Context

Each bot's document chunks and their embeddings lived in Chroma, embedded in the Python process and saved to the `chroma_db/` folder. ADR 0001 already named the limit: it suits one process. In Step 9 Lampus runs several API copies, which must all search the same documents, and everything else the engine stores is already in Postgres (ADRs 0025 and 0026).

Course topic 20 (vector databases and ANN indexes) also asked a question this step had to answer with numbers: does Lampus need an approximate index such as HNSW?

## Decision

- Store chunks in one Postgres table, `chunks` (`engine/store/vectors.sql`): the bot's collection name, the chunk id, text, metadata as JSONB, and the 384-number embedding as a pgvector `vector(384)`. The primary key `(collection, id)` is also the index that finds one bot's chunks.
- `engine/vectorstore.py` now holds a small `VectorStore` class with the methods the engine used from Chroma (`reset_collection`, `add_documents`, `similarity_search`, `get`, `count`). Retrieval (BM25, hybrid, reranking) is unchanged.
- **No HNSW index for now: every search is exact, inside one bot.** Measured below: at Lampus's size an exact search is as fast as Chroma's and never misses, while an HNSW index on a table shared by many bots silently lost most results.
- Install the extension in the `public` schema, so a throwaway test schema can be dropped without removing it.
- `engine/migrate_chroma.py` copies every Chroma collection with its stored embeddings (no re-embedding) and checks the counts.

## Measurements

**Real bots** (`loadtest/bench_vectors.py`; 43 eval questions over 3 bots of 26 to 142 body chunks; vector search only, top 10):

| Store | Search p50 / p95 | recall@10 vs exact |
| --- | --- | --- |
| Chroma (before) | 2.81 ms / 3.73 ms | 1.0 |
| Postgres, exact per bot (after) | 2.15 ms / 2.99 ms | 1.0 |

The two stores' top 10 agree on 100% of the questions.

**At a size where an index matters** (`loadtest/bench_hnsw.py`; synthetic: 50,000 vectors made from the real chunk embeddings plus noise, spread over 50 bots; 43 real questions; pgvector 0.6.2 on a dual-core laptop). HNSW built with pgvector's defaults (m 16, ef_construction 64) in 14 s; the index (102 MB) is bigger than the rows (82 MB).

| Search | recall@10 | p50 |
| --- | --- | --- |
| Whole table, exact | 1.0 | 42 ms |
| Whole table, HNSW, ef_search 10 / 40 / 160 / 320 | 0.51 / 0.68 / 0.84 / 0.92 | 2.1 / 2.7 / 4.2 / 7.1 ms |
| One bot (1,000 chunks), exact | 1.0 | 2.3 ms |
| One bot, HNSW then filter, ef_search 40 | 0.07 (0.7 rows of 10 returned) | 1.5 ms |
| One bot, HNSW then filter, ef_search 160 | 0.29 (2.9 rows) | 3.0 ms |

- HNSW is 6 to 20 times faster than reading every row, at the price of missing some true neighbours; ef_search is the dial between the two. The synthetic data is harder than real text (many near-identical distances), so treat these recall figures as pessimistic.
- **The filter problem:** HNSW walks the graph to the ef_search nearest vectors of the whole table and only then drops other bots' rows. With 50 bots, about 2% survive. Postgres's planner picked this plan by itself for the one-bot query once the index existed, so retrieval would have quietly returned almost nothing.

## Alternatives considered

- **HNSW plus pgvector 0.8's iterative scan** (`hnsw.iterative_scan`), which keeps walking until enough rows pass the filter: the right tool once one bot holds tens of thousands of chunks. Not needed at a few thousand per bot, and not testable on this laptop's pgvector 0.6.2.
- **One partial index or partition per bot:** keeps HNSW and isolation, but means DDL for every new bot.
- **Keep Chroma as a server:** works with several API copies, but adds a second database to run, back up and secure.
- **Qdrant or a hosted vector database:** strong filtering built in, but another system and cost; Postgres already runs.

## Consequences

- Search latency on the real bots 2.81 ms → 2.15 ms (p50), same results. Every API copy now searches the same documents.
- Exact search grows with a bot's size (about 42 ms at 50,000 chunks). Revisit when any bot passes about 10,000 chunks: add HNSW with iterative scan and measure recall again.
- `chromadb` and `langchain-chroma` stay in requirements only for the one-time migration; remove them after Phase 1.
- Tests run in a schema with `public` on the search path (where the extension lives).
