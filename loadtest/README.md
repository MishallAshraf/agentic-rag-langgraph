# Load tests

Phase 1 of Lampus moves the engine from "one process on one machine" to "several copies behind a load balancer". These load tests measure the platform before and after each step, so every change has a number.

The model is faked (`mock_llm.py`): it speaks the OpenAI API with a fixed delay (400 ms to the first token, 15 ms per token after that; illustrative values, roughly a small hosted model). So runs cost nothing, repeat exactly, and show the platform rather than the model's speed. The fake still exercises the real code: the input guard, the agent, retrieval with reranking over the café bot's documents, the grader and the fact check (5 model calls per turn).

## How to run

Three terminals in the repository root, with Postgres and Redis running (`docker compose up -d db redis`). The API uses its own `loadtest` schema in Postgres, so your real users and conversations are untouched. Create the schema once:

```powershell
docker compose exec db psql -U lampus -c "CREATE SCHEMA IF NOT EXISTS loadtest"
```

```powershell
# 1. the fake model
.\.venv\Scripts\python.exe loadtest\mock_llm.py
```

```powershell
# 2. the API with load-test settings (limits raised so they don't hide the platform's own behaviour)
$env:DATABASE_URL="host=localhost user=lampus password=lampus dbname=lampus options='-c search_path=loadtest,public'"
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


### Step 5: documents from Chroma to Postgres with pgvector

Same laptop, 2026-11-25. See [ADR 0027](../docs/adr/0027-pgvector-as-vector-store.md).

**Real bots** (`bench_vectors.py`): 43 eval questions over 3 bots, vector search only (top 10), checked against an exact numpy search.

| Store | Search p50 / p95 | recall@10 |
| --- | --- | --- |
| Chroma (before) | 2.81 ms / 3.73 ms | 1.0 |
| Postgres, exact inside the bot (after) | 2.15 ms / 2.99 ms | 1.0 |

Both stores return the same top 10 for all 43 questions.

**HNSW vs exact at 50,000 chunks** (`bench_hnsw.py`, synthetic vectors built from the real embeddings plus noise, 50 bots):

| Search | recall@10 | p50 |
| --- | --- | --- |
| Whole table, exact | 1.0 | 42 ms |
| Whole table, HNSW, ef_search 10 / 20 / 40 / 80 / 160 / 320 | 0.51 / 0.59 / 0.68 / 0.76 / 0.84 / 0.92 | 2.1 / 2.3 / 2.7 / 3.5 / 4.2 / 7.1 ms |
| One bot, exact | 1.0 | 2.3 ms |
| One bot, HNSW then filter, ef_search 40 / 160 | 0.07 / 0.29 | 1.5 / 3.0 ms |

HNSW trades recall for speed, and ef_search sets the trade. But filtered to one bot, it finds the nearest chunks of all bots first and then discards 98% of them, returning 0.7 of 10 rows on average, and the planner chose that plan by itself. Lampus's bots are small enough for exact search, so the product has no HNSW index yet.

The first run of this benchmark reported perfect recall for the filtered HNSW search. psycopg had prepared the repeated query, and Postgres kept reusing the exact plan after the planner settings changed. The benchmark now turns prepared statements off.

### Step 6: data lineage and incremental ingestion

Same laptop, 2026-11-25. See [ADR 0028](../docs/adr/0028-data-lineage-and-incremental-ingestion.md).

`bench_ingest.py` ingests a copy of the 28 ADRs (224 chunks) three times: first, again with nothing changed, and after one paragraph is added to one file.

| Run | Before: chunks embedded, time | After: chunks embedded, time |
| --- | --- | --- |
| First ingest | 224, 108 s | 224, 71 s |
| Nothing changed | 224, 194 s | 0, 0.3 s |
| One file edited | 225, 130 s | 1, 0.4 s |

Before, every ingest deleted the collection and embedded everything again. Now an unchanged file is recognised by its hash and skipped, and in an edited file only chunks whose text changed are embedded. The before times vary a lot (108 s to 194 s for the same work): the laptop's CPU is shared with everything else running on it.

### Step 7: shared state in Redis, measured with two API copies

GitHub Codespace (2 cores, Linux), 2026-11-25. See [ADR 0029](../docs/adr/0029-redis-for-shared-limits-locks-and-cache.md).

From this step on, the load tests run in the Codespace, which has Docker for Postgres and Redis. Start the fake model and N API copies (ports 8001, 8002, ...) with one script, then run the check:

```bash
API_RATE_LIMIT=10 bash loadtest/start_copies.sh 2
python loadtest/bench_shared_state.py
bash loadtest/start_copies.sh stop
```

The script also ingests the café bot into the `loadtest` schema: since Step 5 the documents are in Postgres, so each schema needs its own chunks.

| Check | Before: memory in each copy | After: Redis |
| --- | --- | --- |
| One student sends 30 messages in a burst over both copies (limit 10 a minute) | 20 started | 10 started, 20 refused (429) |
| Two messages to one conversation at the same moment, one per copy | both ran | 1 ran, 1 refused (409) |

Starting two copies at once on a fresh database first hung both forever. LangGraph's table setup builds an index with `CREATE INDEX CONCURRENTLY`, which waits for every open transaction to finish, including the other copy's blocking wait for the setup lock. `engine/db.py` now polls for that lock instead.

### Step 8: bot builds through a queue to a worker

GitHub Codespace (2 cores, Linux), 2026-11-25. See [ADR 0030](../docs/adr/0030-build-queue-and-worker.md).

`start_copies.sh` now also starts the build workers (`WORKERS=1` by default) and turns off the worker inside the API copies. `bench_builds.py` signs in an admin on both copies, starts a build of the café bot on copy A, checks that copy B can see it, kills a process two seconds in, and watches the build from copy B:

```bash
bash loadtest/start_copies.sh 2
python loadtest/bench_builds.py                  # kills API copy A
bash loadtest/start_copies.sh stop
QUEUE_CLAIM_IDLE_MS=10000 WORKERS=2 bash loadtest/start_copies.sh 2
python loadtest/bench_builds.py --kill worker    # kills the worker running the build
```

| Check | Before: build in the API's memory | After: queue + worker |
| --- | --- | --- |
| Copy B sees a build started on copy A | no (404) | yes |
| Copy A killed 2 s in | build lost (404 on B after 120 s) | finished 11 s later |
| The worker running it killed 2 s in, a second worker idle | (no worker) | finished 17 s later, on attempt 2 |

With the fake model the builder's drafts never pass its checks, so finished builds end as `failed` after three drafts. What counts is that the build ran to its end. In the worker test the second worker took the job over 10 s after the first stopped touching it (60 s by default), and started the build again.

### Step 9: three copies behind nginx, a read replica, a copy killed

GitHub Codespace (2 cores, Linux), 2026-11-25. See [ADR 0031](../docs/adr/0031-scale-out-nginx-and-read-replica.md).

`SCALE=1` also starts nginx on port 8000 in front of the copies and a fresh read replica on port 5433, and gives the copies `DATABASE_READ_URL`:

```bash
SCALE=1 WORKERS=0 bash loadtest/start_copies.sh 3
python loadtest/run.py run --url http://127.0.0.1:8000 --users 50 --turns 3
python loadtest/bench_replica.py --watch 45      # during a run: where the searches went, and the replica's lag
python loadtest/bench_failover.py                # 20 students; copy 1 killed after 15 s
bash loadtest/start_copies.sh stop
```

50 students × 3 messages, the search cache cleared before each run:

| Setup | Completed | Turns/s | Median turn | p95 turn |
| --- | --- | --- | --- | --- |
| 1 copy, before the fix | 0 of 100 started; hung | 0 | - | - |
| 1 copy | 150 | 2.32 | 15.4 s | 24.5 s |
| 1 copy, 32 tool threads | 102 (136 on a rerun) | 1.26 (1.74) | 5.0 s | 6.4 s |
| 3 copies behind nginx, read replica | 150 | 2.38 | 7.4 s | 38.9 s |

The first run hung one copy for good: the search graph, run in the agents' 6 tool threads, saved its steps through the event loop, and every thread ended up blocked. It no longer saves them. After the fix, three copies halve the median turn (18 tool threads instead of 6) but don't raise throughput: with the search cache off, one copy alone keeps both cores busy, so copies on one machine only share the same CPU. 32 threads in one copy moved the queue to the database pool (`PoolTimeout`).

A copy killed after 15 s, 20 students chatting without pause, 45 s watched:

| Setup | Replies cut off | Students answered again | Longest wait for an answer |
| --- | --- | --- | --- |
| 1 copy | 20 | 0 of 20 | never |
| 3 copies, 3-minute locks (Step 7) | 7 | 13 of 20 | 7 locked out to the end |
| 3 copies, 15 s renewed locks | 6 | 20 of 20 | 19.4 s |

Searches with the cache off: 25 of 25 chunk reads on the primary without a replica; 3 of 33 with one (each copy loading its keyword index). The replica was 1.9 ms behind the primary at the median (27 ms p95), and 14 ms (161 ms p95) with the CPU saturated.
