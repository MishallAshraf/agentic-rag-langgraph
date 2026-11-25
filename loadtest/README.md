# Load tests

Phase 1 of Lampus moves the engine from "one process on one machine" to "several copies behind a load balancer". These load tests measure the platform before and after each step, so every change has a number.

The model is faked (`mock_llm.py`): it speaks the OpenAI API with a fixed delay (400 ms to the first token, 15 ms per token after that; illustrative values, roughly a small hosted model). So runs cost nothing, repeat exactly, and show the platform rather than the model's speed. The fake still exercises the real code: the input guard, the agent, retrieval with reranking over the café bot's documents, the grader and the fact check (5 model calls per turn).

## How to run

Three terminals in the repository root, with Postgres running (`docker compose up -d db`). The API uses its own `loadtest` schema in Postgres, so your real users and conversations are untouched. Create the schema once:

```powershell
docker compose exec db psql -U lampus -c "CREATE SCHEMA IF NOT EXISTS loadtest"
```

```powershell
# 1. the fake model
.\.venv\Scripts\python.exe loadtest\mock_llm.py
```

```powershell
# 2. the API with load-test settings (limits raised so they don't hide the platform's own behaviour)
$env:DATABASE_URL="host=localhost user=lampus password=lampus dbname=lampus options='-c search_path=loadtest'"
$env:LLM_BASE_URL="http://127.0.0.1:8900/v1"; $env:API_RATE_LIMIT="1000"
$env:QUOTA_TURNS_PER_DAY="100000"; $env:QUOTA_TOKENS_PER_DAY="100000000"
.\.venv\Scripts\python.exe loadtest\run.py setup --users 50
.\.venv\Scripts\python.exe -m engine.api
```

```powershell
# 3. the load: N students at once, each sending 3 messages in a row
.\.venv\Scripts\python.exe loadtest\run.py run --users 10 --turns 3 --label "what this run measures"
```

Every run is appended to `results.jsonl`.

## What is measured

- **Time to first token (TTFT):** from sending a message until the first word of the answer arrives.
- **Turn time:** until the final, checked answer arrives.
- **Throughput:** completed turns per second across all students.
- **Errors:** anything that isn't a complete answer.

p50 is the typical case; p95 is the slow end (1 in 20 is slower).

## Results

### Baseline (Step 0): 1 API process, SQLite + Chroma, all shared state in memory

Windows laptop, 2026-11-25. Before any Phase 1 change.

| Students at once | Turns attempted | Completed | Errors | TTFT p50 / p95 | Turn p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| 1 | 3 | 3 | none | 1.42 s / 2.71 s | 1.99 s / 3.28 s |
| 10 | 30 | 3 | 9 of 10 sign-ins failed (HTTP 500) | 1.43 s / 1.47 s | 1.98 s / 2.03 s |
| 25 | 75 | 3 | 24 of 25 sign-ins failed (HTTP 500) | 1.41 s / 1.46 s | 1.98 s / 1.98 s |

The first turn after starting the API took 11 s (cold start: loading the embedding and reranking models).

**Finding: the platform breaks with more than one student at a time.** The cause is in `auth.py`: every request shares **one** SQLite connection, and `audit()` opens a transaction (`BEGIN IMMEDIATE`), then awaits, then commits. While it waits, another request on the same connection tries to `BEGIN` and gets `sqlite3.OperationalError: cannot start a transaction within a transaction`. Worse, the failing request's `ROLLBACK` can undo the *other* request's transaction. Every sign-in and every turn writes an audit entry, so concurrent students collide.

This is the "before" number for Step 3 (Postgres with a connection pool and a transaction per request; course topics 4 and 6): **sign-in failure rate at 10 students at once: 90%**.

### Step 3: users, sessions, quotas and the audit log in Postgres, with a connection pool

Same laptop and settings, 2026-11-25. Postgres 16 on the same machine. Still one API process; conversation checkpoints still in SQLite (Step 4) and documents still in Chroma (Step 5).

| Students at once | Turns attempted | Completed | Errors | Throughput | TTFT p50 / p95 | Turn p50 / p95 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3 | 3 | none | 0.43 turns/s | 1.31 s / 2.47 s | 1.78 s / 2.91 s |
| 10 | 30 | 30 | none | 1.23 turns/s | 1.67 s / 9.82 s | 2.10 s / 10.43 s |
| 25 | 75 | 75 | none | 1.60 turns/s | 3.45 s / 5.40 s | 6.63 s / 7.85 s |
| 50 | 150 | 150 | none | 1.88 turns/s | 6.08 s / 8.09 s | 9.52 s / 13.06 s |

**Before → after: sign-in failures at 10 students at once, 90% → 0%.** Every turn at 10, 25 and 50 students completed (baseline: 3 of 30 and 3 of 75).

What the numbers show next:
- **Throughput levels off at about 1.9 turns per second.** More students only make everyone wait longer (typical turn 1.8 s for one student, 9.5 s for 50). One process on a dual-core laptop is now the limit: retrieval and reranking run on its CPU. This is the "before" for Step 9 (several API copies behind a load balancer; topics 1 and 2).
- **The slow tail at 10 students (p95 9.8 s) most likely comes from sign-in (not yet confirmed).** Password checks (bcrypt, deliberately slow) run on the event loop, so while ten students sign in at once, the first students' turns wait. Moving them to a worker thread is a small follow-up, recorded for the latency topic (54).

### Step 4: conversations (LangGraph checkpoints) in Postgres

Same laptop, 2026-11-25. Everything the API stores is now in Postgres; documents are still in Chroma (Step 5).

**The conversation store on its own** (`bench_checkpoints.py`): N conversations at once, each doing 10 turns of "read the latest checkpoint, save 4 new ones" with growing history, no model.

| Conversations at once | SQLite: operations/s | SQLite: one turn's saving, p50 / p95 | Postgres: operations/s | Postgres: one turn's saving, p50 / p95 |
| --- | --- | --- | --- | --- |
| 1 | 419 | 11 ms / 25 ms | 311 | 12 ms / 35 ms |
| 10 | 465 | 104 ms / 133 ms | 581 | 80 ms / 107 ms |
| 50 | 467 | 550 ms / 631 ms | 692 | 367 ms / 399 ms |

**Before → after at 50 conversations: one turn's saving 550 ms → 367 ms (p50), throughput +48%.** SQLite stays flat at about 465 operations per second however many conversations there are: they all queue for its one connection. Postgres keeps gaining as conversations are added, up to what a dual-core laptop running both the benchmark and the database allows. For a single conversation, SQLite is a little faster: it's a file in the same process, with no network round trip.

Pool size at 50 conversations: a maximum of 4 connections gave 577 operations/s, 10 gave 692, 20 gave 604. More connections than the CPU can serve just compete with each other, so the pool stays at 10.

**The whole API** (`run.py`, same settings as Step 3):

| Students at once | Completed | Errors | Throughput | TTFT p50 / p95 | Turn p50 / p95 |
| --- | --- | --- | --- | --- | --- |
| 1 | 3 of 3 | none | 0.43 turns/s | 1.30 s / 2.54 s | 1.77 s / 3.02 s |
| 10 | 30 of 30 | none | 1.27 turns/s | 1.76 s / 8.99 s | 2.34 s / 9.74 s |
| 25 | 75 of 75 | none | 1.93 turns/s | 2.67 s / 4.85 s | 4.35 s / 5.87 s |
| 50 | 150 of 150 | none | 1.90 turns/s | 4.82 s / 9.80 s | 10.06 s / 10.93 s |

At 25 students, the typical turn went from 6.6 s (Step 3) to 4.4 s and throughput from 1.60 to 1.93 turns/s. At 50 students nothing changed (1.88 → 1.90 turns/s): the single API process's CPU, busy with retrieval and reranking, is the limit there, not storage. That is Step 9's job.

