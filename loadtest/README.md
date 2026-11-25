# Load tests

Phase 1 of Lampus moves the engine from "one process on one machine" to "several copies behind a load balancer". These load tests measure the platform before and after each step, so every change has a number.

The model is faked (`mock_llm.py`): it speaks the OpenAI API with a fixed delay (400 ms to the first token, 15 ms per token after that; illustrative values, roughly a small hosted model). So runs cost nothing, repeat exactly, and show the platform rather than the model's speed. The fake still exercises the real code: the input guard, the agent, retrieval with reranking over the café bot's documents, the grader and the fact check (5 model calls per turn).

## How to run

Three terminals in the repository root, with Postgres running (`docker compose up -d db`). The API uses its own `loadtest` schema in Postgres and a throwaway checkpoint file under `data/loadtest/` (ignored by git), so your real users and conversations are untouched. Create the schema once:

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
$env:CHECKPOINT_DB="data/loadtest/checkpoints.sqlite"
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

