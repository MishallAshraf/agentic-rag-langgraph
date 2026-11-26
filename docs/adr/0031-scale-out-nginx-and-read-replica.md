# 0031. Three API copies behind nginx and a Postgres read replica for searches; two crash bugs fixed on the way

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 9

## Context

Steps 3 to 8 moved every piece of state out of the API process: users, conversations and chunks into Postgres; limits, locks and the cache into Redis; builds into a queue. Any copy of the API can now serve any request, so the API can run as several copies (course topic 1, scaling horizontally) behind a load balancer (topic 2). Searches can read from a replica of the database (topic 7, replication).

The questions for this step: what does a second and third copy buy, and what do students see when a copy dies?

All measurements: GitHub Codespace (2 cores, 8 GB), the fake model (`loadtest/mock_llm.py`), 50 students sending 3 messages each (`loadtest/run.py`), the search cache cleared before each run.

## What the first measurement found

**One copy hung for good under 50 students.** None of the first 100 turns completed. 50 hit the 120 s turn limit, and the copy then made no model call for over 10 minutes. A stack dump (py-spy) showed why:

- The agent calls the document search, itself a small LangGraph graph, as a synchronous tool. It runs on the event loop's default thread pool, which has `min(32, cores + 4)` threads: 6 on 2 cores.
- Called inside a conversation, the search graph inherited the conversation's Postgres checkpointer. It saved each of its steps by handing the save to the event loop and blocking its thread until it was done.
- All 6 threads ended up blocked that way, the saves never completed, and the copy stopped.

The search is never paused or resumed, so its steps don't need saving. It is now compiled with `checkpointer=False` (`engine/search_graph.py`), and its tool threads never wait on the event loop. With that fix, one copy completes all 150 turns.

## Decision

- **nginx in front of three API copies** (`deploy/nginx.conf`, the `nginx` service in `docker-compose.yml`):
  - **`least_conn`:** a reply streams for seconds, so a new request goes to the copy with the fewest open requests, rather than simply the next in turn.
  - **`proxy_buffering off`:** streamed replies pass through as they are written.
  - **A copy that refuses a connection** is skipped for 10 s, and the request goes to the next copy. nginx never retries a request that already reached a copy, so a message is never sent twice.
  - **The copies trust `X-Forwarded-For` only from nginx's address** (`FORWARDED_ALLOW_IPS`), so the sign-in limiter still counts by each student's own address.
- **A streaming read replica for searches** (the `db-replica` service, `deploy/replica-entrypoint.sh`):
  - **Setup:** the replica copies the primary once (`pg_basebackup`), then replays every change as it arrives.
  - **What reads there:** with `DATABASE_READ_URL` set, chunk searches go to the replica (`engine/vectorstore.py`). A search can live with a few milliseconds of lag, because a bot's documents don't change between two messages.
  - **What stays on the primary:** sign-ins, quotas, conversations and builds, which must see their own writes at once. So do the build worker's searches, since it reads chunks it has just written.
  - **If the replica doesn't answer within 2 s,** searches use the primary for the next 30 s, then try the replica again.
- **Conversation locks last 15 s and are renewed every 5 s while the turn runs** (`API_LOCK_TTL`). Before, a lock lasted 3 minutes, the longest a turn may take. When a copy died, its students' conversations stayed locked: every retry got a 409 for up to 3 minutes. Renewal stops after the longest a turn may take, so a lock whose release is missed still expires.
- **`API_TOOL_THREADS`** sets the tool thread pool's size. It stays at Python's default: the measurement below shows why.

## Measurements

**Turns, 50 students × 3:**

| Setup | Completed | Turns/s | Median turn | p95 turn |
| --- | --- | --- | --- | --- |
| 1 copy, before the fix | 0 of 100 started; hung | 0 | – | – |
| 1 copy | 150 | 2.32 | 15.4 s | 24.5 s |
| 1 copy, 32 tool threads | 102 (136 on a rerun) | 1.26 (1.74) | 5.0 s | 6.4 s |
| 3 copies behind nginx, read replica | 150 | 2.38 | 7.4 s | 38.9 s |

- **Three copies halve the median turn**, because 18 tool threads instead of 6 means fewer students wait in line.
- **Throughput doesn't rise**, and the mean turn time is the same in both setups (15.6 s). With the cache off, one copy alone keeps both cores at 176%, mostly embedding questions. Copies on one 2-core machine share the same CPU. Adding throughput needs copies on separate machines, which a Codespace can't show.
- **The slowest turns got slower** (p95 24.5 s → 38.9 s). `least_conn` counts open requests, not the CPU work in them, so some copies queue longer than others.
- **32 threads in one copy** made turns fast (median 5 s) but failed 48 turns, and 14 on a rerun. The rerun's log shows all 14 were `PoolTimeout`: 32 threads competing for 10 database connections. Raising one limit moved the queue to the next one, so the setting stays at Python's default.

**A copy killed mid-load** (`loadtest/bench_failover.py`): 20 students chatting without pause, copy 1 killed after 15 s, watched for 45 s more.

| Setup | Replies cut off | Students answered again | Longest wait for an answer |
| --- | --- | --- | --- |
| 1 copy | 20 | 0 of 20 | never |
| 3 copies, 3-minute locks (as in Step 7) | 7 | 13 of 20 | 7 students locked out to the end |
| 3 copies, 15 s renewed locks | 6 | 20 of 20 | 19.4 s |

- **Replies streaming from the dead copy are lost either way.** The student sends again.
- **With 3-minute locks,** the 7 locked-out students' retries also used up the bot's shared rate-limit bucket: 80 requests were refused with 429.

**Searches** (`loadtest/bench_replica.py`, counted per server from `pg_stat_user_tables`, search cache off):

- **Chunk reads on the primary:** 25 of 25 with no replica, 3 of 33 with one. Those 3 are the keyword index each copy loads once from the primary, by design.
- **Replication lag** (from a write on the primary until the replica has replayed it): 1.9 ms median and 27 ms p95 at normal load. At full CPU it was 14 ms median and 161 ms p95.

95 tests pass, including 2 new ones:

- a search falls back to the primary when the replica doesn't answer, and stops waiting for it;
- a renewed lock outlives its first expiry, only its holder can renew it, and it expires once renewal stops.

## Alternatives considered

- **A bigger machine instead of more copies (scaling vertically):** simpler, and in this Codespace the only way to more throughput. But one process is still one point of failure, as the failover table shows. Cloud machines also stop growing at some size and cost.
- **Round robin:** the next copy each time. Streaming turns last from 5 to 60 s, so the copies' open requests drift apart. `least_conn` corrects for that, but the p95 above shows it isn't enough: it doesn't know how heavy each request is.
- **Sticky sessions** (a student always on the same copy): not needed, since every copy can serve every conversation, and they'd put a crashed copy's students in a queue to nowhere.
- **A managed load balancer** (a cloud load balancer, or Kubernetes with a Service): the production equivalent, with active health checks. nginx shows the same mechanics on a laptop at no cost.
- **Routing every read to the replica:** reads that must see their own writes would break. A student just signed in, or the next message right after a saved one, could miss its own data by a few milliseconds.
- **Logical replication or a separate search database:** more moving parts for no gain at this size. Physical streaming replication copies everything, pgvector included.

## Consequences

- One copy dying no longer stops the service: the replies it was streaming are lost, and its students are answered again within about 20 s.
- On one machine, more copies make waiting fairer (median turn halved) but not faster overall. The next real gain is separate machines, or a model server that does the embedding (Phase 2).
- The replica takes the most common reads off the primary. On separate hardware that frees the primary for writes. Here, on one machine, it shows the mechanics and the lag, not a capacity gain.
- nginx has no active health checks: it finds a dead copy when a request to it fails. That's acceptable with `proxy_next_upstream`, since a refused connection is retried on another copy before the student sees it.
- `docker-compose.yml` points Postgres at `deploy/pg_hba.conf` to allow replication connections. If that file is missing, Docker creates a folder in its place and Postgres doesn't start, as happened once in the Codespace.
