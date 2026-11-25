# 0029. Redis for rate limits, conversation locks and the search cache

- Status: Accepted; the conversation lock's 3-minute lifetime is superseded by [ADR 0031](0031-scale-out-nginx-and-read-replica.md) (15 s, renewed while the turn runs)
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 7

## Context

Three pieces of the API's state lived in each process's memory:

- the rate limiter (a sliding window per user: 10 messages a minute),
- the lock that lets only one turn run in a conversation at a time,
- the cache of graded searches (an in-memory LRU of 256 entries).

That is correct for one process. Step 9 runs several API copies behind a load balancer, and then each copy keeps its own count, its own locks and its own cache. Measured with two copies (`loadtest/bench_shared_state.py`, in a Codespace):

- a student limited to 10 messages a minute got 20 of 30 started;
- two messages sent at the same moment to one conversation, one per copy, both ran. Two turns writing one conversation at once can interleave its saved state.

## Decision

Keep this state in Redis (`engine/shared.py`), which every copy talks to:

- **Rate limits (course topic 8): token buckets.** Each student has a bucket of 10 tokens that refills at 10 a minute. Each bot, which in Lampus is one school's tutor, has a bucket of 300 a minute, so one busy school can't use up capacity every school shares. A message takes a token from both buckets or from neither. One Lua script does the whole check, and Redis runs a script without interleaving other commands, so two copies can never both take the last token. The script uses Redis's clock, so copies on machines whose clocks differ still agree.
- **Sign-in failures:** a counter per address and username with a 60-second expiry; blocked after 5 failures.
- **Conversation locks:** `SET key token NX PX ttl`. The lock expires after the longest a turn may take (turn timeout + 60 s), so a copy that crashes mid-turn can't lock a conversation forever. It is released only by the holder of its random token (a short Lua compare-and-delete).
- **Search cache (course topic 3):** cache-aside. Look in Redis first; on a miss, run the search and store the result for 24 hours. Redis is capped at 256 MB and evicts the least recently used keys (`allkeys-lru`). Each bot's keys carry a generation number, which ingestion increments when a bot's files change (Step 6's lineage), so every copy stops using results that quote old text at once.
- **When Redis is down:**
  - Rate limiting and the cache fail open. Requests go through, searches run uncached, and the daily quota in Postgres still caps each student.
  - Locks fail closed, because the request errors out instead of risking two turns in one conversation.
- Redis keeps no data on disk (`--save ""`): everything in it can be lost without harm. Limits refill, locks expire and the cache refills.

## Measurements

Two API copies sharing Postgres and Redis, a fake model, `API_RATE_LIMIT=10` (`loadtest/bench_shared_state.py`, GitHub Codespace, 2 cores):

| Check | Before: memory in each copy | After: Redis |
| --- | --- | --- |
| 30 messages in a burst from one student, limit 10 a minute | 20 started | 10 started, 20 refused (429) |
| 2 messages to one conversation at the same moment, one per copy | 2 ran | 1 ran, 1 refused (409) |

90 tests pass, including 8 new ones: 50 concurrent requests for 10 tokens get exactly 10, 20 racers for one lock get exactly 1, a crashed holder's lock expires, and re-ingesting a bot invalidates only its cache.

## Alternatives considered

- **Postgres for all of it:** already running, and advisory locks would work. But rate limits and cache reads are on every request, and short-lived counters with expiry are what Redis is built for. They would add write load and table bloat to the database that holds the data that matters.
- **Sticky sessions** (the load balancer always sends a student to the same copy): keeps memory state working for the lock, but not for a per-school limit, and a copy restart still loses everything.
- **Sliding-window log in Redis:** exact, but stores every request. A token bucket stores two numbers per key, and allows a short burst, which suits a student sending a few quick follow-ups.
- **Redlock (locks across several Redis servers):** stronger guarantees, but needs 3 or more Redis servers. One Redis plus the lock's expiry is enough here.

## Consequences

- The limits and the one-turn-per-conversation rule now hold however many API copies run (Step 9).
- Redis is a new service to run: in `docker-compose.yml`, the Codespace and CI. The terminal agents still work without it: their searches just aren't cached.
- If a client disconnects before the server starts streaming a reply, the conversation stays locked until the lock expires (up to 3 minutes).
- Found while starting two copies at once: LangGraph's table setup (`CREATE INDEX CONCURRENTLY`) waits for every open transaction, including the other copy's blocking wait for the setup lock, so both copies hung forever on a fresh database. `engine/db.py` now polls for that lock (`pg_try_advisory_lock`) instead of blocking.
