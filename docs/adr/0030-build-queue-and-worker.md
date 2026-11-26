# 0030. Bot builds through a Redis Streams queue to a separate worker, with progress in Postgres and pub/sub notifications

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 8

## Context

Step 15 ([ADR 0020](0020-bot-builder-as-a-service.md)) runs a bot build as a background task inside the API process. The build and its progress events live in that process's memory, and the builder's paused state (waiting for the admin's review) is in an in-memory checkpointer.

That works for one API copy. With several copies behind a load balancer (Step 9) it breaks in two ways, measured with two copies and a fake model (`loadtest/bench_builds.py`, in a Codespace):

- An admin starts a build on copy A, and their next page load reaches copy B. Copy B answers 404: it has never heard of the build.
- Copy A is killed two seconds into the build. The build is gone. Two minutes later copy B still answers 404, and the uploaded files sit in a folder no build will ever use.

A build also takes minutes of CPU and memory (embedding documents, several LLM calls). Inside the API it competes with students' chats for the same process.

## Decision

Course topic 5, message queues and pub/sub:

- **Builds and their events are rows in Postgres** (`builds`, `build_events` in `engine/store/schema.sql`). Every copy reads the same rows. Each event has a number per build, so a page that reconnects asks for "everything after event n", from any copy.
- **The API only queues the job.** `POST /admin/builds` stores the uploads, inserts the build as `queued` and adds `{"build": id}` to a Redis Stream (`engine/shared.py`: `enqueue`). It no longer refuses a second build while one runs: builds wait in the queue.
- **A worker runs it** (`engine/worker.py`, `python -m engine.worker`). Workers form one consumer group, so each job goes to exactly one of them (`XREADGROUP`). A worker runs one build at a time, writes progress events as it goes, and acknowledges the job (`XACK`) only when the build has reached review or failed.
- **A worker that dies is replaced without anyone noticing.** While a build runs, its worker touches the job every 20 s. A job nobody has touched for 60 s (`QUEUE_CLAIM_IDLE_MS`) is taken over by the next worker that asks (`XAUTOCLAIM`), which starts the build again. The build counts its attempts and gives up after 3, so a build that crashes every worker can't loop forever.
- **Delivery is at least once, so running a job is safe twice.** A worker first checks the build's status in Postgres and skips builds that already finished. Each attempt writes its checkpoints under its own thread id.
- **The review pause is in the shared Postgres checkpointer**, not in memory. The worker pauses the builder graph at the review. Whichever copy receives the admin's decision resumes it. Two admins deciding at once can't both win: the status changes from `review` to `deciding` in one conditional `UPDATE`.
- **Pub/sub only says "something changed".**
  - After writing an event, the writer publishes on `build:<id>`. Copies streaming that build's progress wake up and read the new events from Postgres.
  - When a bot is approved, the deciding copy publishes on `bots`, and every other copy starts serving the bot.
  - Pub/sub delivers only to whoever is listening at that moment, so nothing depends on a message arriving. Listeners also re-check Postgres every 15 s.
- **One command still works on a laptop:** the API runs the worker loop inside itself unless `API_RUN_WORKER=0`. With separate workers, set it to 0 on every API copy.

## Measurements

Two API copies sharing Postgres and Redis, a fake model, the café bot's two documents (`loadtest/bench_builds.py`, GitHub Codespace, 2 cores). The fake model's drafts fail the builder's checks three times, so every build that finishes ends as `failed`. The point is whether a build finishes at all and who can see it.

| Check | Before: build in the API's memory | After: queue + worker |
| --- | --- | --- |
| Copy B can see a build started on copy A | no (404) | yes |
| Copy A killed 2 s into the build | lost: still 404 on copy B after 120 s | finished, 11 s later (the worker was not touched) |
| The worker running the build killed 2 s in, a second worker running (`QUEUE_CLAIM_IDLE_MS=10000`) | (no worker before) | finished, 17 s later, on attempt 2 |

93 tests pass, including 3 new ones:

- a job goes to one worker, is taken over once its worker stops touching it, and is never handed out after it is acknowledged;
- one publish reaches every subscriber;
- two admins deciding the same build at once: one wins.

The build tests now run every build through the queue.

## Alternatives considered

- **Celery, RQ or Dramatiq:** mature task queues with retries, scheduling and monitoring. But they add a framework for one kind of job, and the build already needs its own state in Postgres for progress and review. Redis Streams covers the queue in about 60 lines.
- **A Postgres queue** (`SELECT ... FOR UPDATE SKIP LOCKED`): one fewer moving part, and transactional with the build row. A good choice too. Redis was already there from Step 7, and Streams give consumer groups and takeover of idle jobs out of the box, which is what this topic is about.
- **Redis lists** (`LPUSH` and `BRPOP`): simpler, but a popped job is gone. If the worker dies, the job is lost unless a second "processing" list and a sweeper are built by hand. Streams keep the job pending until it is acknowledged.
- **Kafka or a managed queue (SQS, Pub/Sub):** built for far more traffic than a few builds a day, and they cost money or another service to run.
- **Pub/sub alone for progress:** a page that connects after an event was published never sees it. Postgres keeps the events, and pub/sub only wakes the readers.

## Consequences

- A build survives the copy that started it and the worker running it. A dead worker's build starts again after about a minute, from the beginning: a build is short enough that resuming mid-way isn't worth it.
- Several builds can wait at once. Throughput is the number of workers, each running one build.
- A second process to run in production (`python -m engine.worker`), with the same settings as the API.
- Progress events are written to Postgres one by one, a handful per build: negligible.
- Old builds stay in the `builds` table. Nothing cleans them up yet.
- Found while testing: in redis-py, `get_message(ignore_subscribe_messages=True)` returns `None` at once when the next message is the subscription confirmation, instead of waiting. `shared.subscribe()` waits for the confirmation before returning, so a message published right after it can't be missed.
