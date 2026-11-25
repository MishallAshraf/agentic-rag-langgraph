# 0026. Conversations (LangGraph checkpoints) in Postgres

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 4
- Supersedes the storage part of: [0009](0009-sqlite-checkpoints-and-summary-compaction.md) (summary compaction is unchanged)

## Context

LangGraph saves a checkpoint of each conversation after every step, so a conversation can resume after a restart, or after a pause for human approval. The checkpoints lived in `data/checkpoints.sqlite` through `AsyncSqliteSaver`, which uses one connection guarded by a lock. Two problems for Lampus:

- **Concurrency:** every conversation's reads and writes queue for that one connection. A benchmark of the store alone stayed flat at about 465 operations per second however many conversations ran, and one turn's saving grew from 11 ms (1 conversation) to 550 ms (50).
- **Several API copies (Step 9):** a SQLite file on one machine can't be shared, so a student whose next message reached another copy would lose the conversation.

Users and the audit log already moved to Postgres in Step 3 ([ADR 0025](0025-postgres-for-users-quotas-and-audit.md)).

## Decision

- Use LangGraph's `AsyncPostgresSaver` (`langgraph-checkpoint-postgres`) on its own psycopg pool: `engine.db.postgres_checkpointer()`. The API and the terminal agents (Steps 5, 7, 9) all use it, so they see the same conversations.
- A separate pool from the auth store, so a burst of checkpoint writes can't hold up sign-ins.
- Run `saver.setup()` (LangGraph's table creation and migrations) under a session advisory lock, so two processes starting together don't race.
- Turn off server-side prepared statements on the pools (`prepare_threshold=None`): the schema script holds several statements, and it keeps the pools usable behind a connection proxy such as PgBouncer later.
- Pool size stays at a maximum of 10. Measured at 50 conversations: 4 connections gave 577 operations/s, 10 gave 692, 20 gave 604.
- `engine/migrate_sqlite_checkpoints.py` copies existing conversations oldest checkpoint first, with pending writes (such as a tool call waiting for approval). It checks that every conversation's latest state matches, and skips conversations already copied.

On course topic 4: checkpoints are document-shaped (a whole conversation state, read and written as one unit), the kind of data often put in a NoSQL store. LangGraph stores them in Postgres as JSONB and binary blobs. Keeping them there means one database to run, back up and secure, with no second system for a small team.

## Alternatives considered

- **Keep SQLite:** fine for one process; can't be shared by several API copies, and queues under load.
- **Redis or MongoDB checkpointers:** a document store fits the data's shape, but it adds a second database to run, back up and secure. Redis arrives in Step 7 as a cache, not as the store of record for conversations.
- **One pool for everything:** fewer connections, but checkpoint bursts would compete with sign-ins and quota checks.

## Consequences

- The store alone, 50 conversations at once: one turn's saving 550 ms → 367 ms (p50), throughput +48% (467 → 692 operations/s). A single conversation is slightly slower than SQLite (a network round trip instead of a file in the same process).
- Whole API: at 25 students the typical turn went from 6.6 s to 4.4 s; at 50 students throughput is unchanged (1.9 turns/s), because the single process's CPU is now the limit (Step 9).
- Checked on a copy of the real data: 553 checkpoints in 89 conversation entries copied, with every latest state matching. A second run skips them all.
- All of the engine's state is now in Postgres except documents (Chroma, Step 5) and in-memory pieces such as the rate limiter, cache and locks (Step 7).
