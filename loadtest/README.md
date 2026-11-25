# Load tests

Phase 1 of Lampus moves the engine from "one process on one machine" to "several copies behind a load balancer". These load tests measure the platform before and after each step, so every change has a number.

The model is faked (`mock_llm.py`): it speaks the OpenAI API with a fixed delay (400 ms to the first token, 15 ms per token after that; illustrative values, roughly a small hosted model). So runs cost nothing, repeat exactly, and show the platform rather than the model's speed. The fake still exercises the real code: the input guard, the agent, retrieval with reranking over the café bot's documents, the grader and the fact check (5 model calls per turn).

## How to run

Three terminals in the repository root. The API uses its own throwaway databases under `data/loadtest/` (ignored by git), so your real users and conversations are untouched.

```powershell
# 1. the fake model
.\.venv\Scripts\python.exe loadtest\mock_llm.py
```

```powershell
# 2. the API with load-test settings (limits raised so they don't hide the platform's own behaviour)
$env:AUTH_DB="data/loadtest/auth.sqlite"; $env:CHECKPOINT_DB="data/loadtest/checkpoints.sqlite"
$env:LLM_BASE_URL="http://127.0.0.1:8900/v1"; $env:API_RATE_LIMIT="1000"
$env:QUOTA_TURNS_PER_DAY="100000"; $env:QUOTA_TOKENS_PER_DAY="100000000"
.\.venv\Scripts\python.exe loadtest\run.py setup --users 50
.\.venv\Scripts\python.exe api.py
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
