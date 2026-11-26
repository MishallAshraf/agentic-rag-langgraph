# 0025. Postgres for users, sessions, quotas and the audit log

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 3
- Supersedes the storage part of: [0018](0018-authentication-authorization-quotas-audit.md)

## Context

Users, sessions, bot grants, conversation ownership, daily quotas and the audit log lived in one SQLite file, opened through **one** connection shared by every request. The Step 0 load test showed what that does under load: with 10 students signing in at once, 9 of 10 sign-ins failed with `cannot start a transaction within a transaction`. The audit write opens a transaction and awaits; meanwhile another request on the same connection starts its own. A failing request's `ROLLBACK` could even undo another request's writes.

Two more problems would surface as soon as the engine runs as several processes (Step 9): a SQLite file can't be shared by API copies on different machines, and the quota check (`over_quota`, then later `add_usage`) is a check-then-act race. Two parallel conversations could both see "one turn left" and both go ahead.

## Decision

- Move this data to **Postgres** (course topic 4). It is relational (grants and threads reference users), every write must be all-or-nothing, and it needs strict ordering under concurrency. Proper types: `BOOLEAN`, `TIMESTAMPTZ`, `DATE`, `BIGSERIAL`, foreign keys with `ON DELETE CASCADE`, indexes for the two lookups the API makes (sessions by user, threads by owner).
- Use **psycopg 3** with an **async connection pool** (`engine/db.py`, 2–10 connections). Each store method borrows a connection for one unit of work, so requests never share a connection mid-transaction. psycopg is also the driver LangGraph's Postgres checkpointer uses (Step 4), so one driver serves both.
- Make the two consistency-critical writes atomic (course topic 6, strong consistency where it matters):
  - **Quota:** `reserve_turn` counts a turn in one `INSERT … ON CONFLICT DO UPDATE … WHERE turns < limit RETURNING`. The check is part of the write, and Postgres locks the row, so a quota of N lets exactly N turns through. The early `over_quota` check stays as a cheap first refusal.
  - **Audit chain:** each entry is written in its own transaction under `pg_advisory_xact_lock`, so writers queue instead of both linking to the same previous row.
- Keep the audit's `ts` and `detail` as the exact text that was hashed, so the chain can be re-verified byte for byte.
- Create the tables at start-up from `engine/store/schema.sql` (idempotent, under an advisory lock so two starting processes don't race). A migration tool (Alembic) can come when the schema first changes.
- `engine/migrate_sqlite_auth.py` copies existing data across once, keeping audit hashes, so the chain stays verifiable across the move.
- Tests run against Postgres: each run creates a throwaway schema and drops it at exit. CI starts Postgres as a service container; `docker-compose.yml` and the Codespace start the same image (`pgvector/pgvector:pg16`, ready for Step 5).
- On Windows, psycopg's async mode can't use the default Proactor event loop, so `engine/db.py` switches Windows to the Selector loop and `python -m engine.api` passes `loop="none"` to uvicorn. Checked that the MCP notes server (a subprocess) still works under it. Linux is unaffected.

## Alternatives considered

- **Keep SQLite and add an asyncio lock around transactions:** fixes the crash in one process, but can't be shared by several API copies (Step 9), and every write would queue behind one lock.
- **asyncpg instead of psycopg:** fast and works with Windows' default loop, but LangGraph's Postgres checkpointer needs psycopg, so it would mean two drivers.
- **An ORM (SQLAlchemy):** more abstraction than seven small tables need; plain SQL keeps the consistency rules visible, which is the point of topic 6.
- **A NoSQL store:** this data is relational and needs transactions; documents read and written whole (checkpoints) are the better NoSQL-shaped fit, handled in Step 4.

## Consequences

- Load test, same laptop, one API process: sign-in failures at 10 students at once went from 90% to 0%. 10, 25 and 50 students now complete every turn (150 of 150 at 50), where the baseline completed 3 of 30.
- New tests (`tests/test_store_concurrency.py`): 50 concurrent audit writes leave one intact chain; 20 parallel turn reservations against a quota of 3 grant exactly 3. Writing them also caught a parameter bug in the new quota code that the older tests never reached.
- Throughput now levels off at about 1.9 turns per second: one process on a dual-core CPU is the next limit (Step 9).
- Running the API now needs Postgres: `docker compose up -d db` (started automatically in a Codespace), or any Postgres in `DATABASE_URL`.
- Existing SQLite data stays in `data/auth.sqlite` until copied with `python -m engine.migrate_sqlite_auth`.
