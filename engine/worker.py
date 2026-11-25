"""Lampus Phase 1, Step 8 - The build worker: runs bot builds taken from the Redis queue.

    python -m engine.worker          # uses DATABASE_URL, REDIS_URL and the model settings in .env

Building a bot (indexing documents, drafting with an LLM) takes minutes and a
lot of CPU and memory. Running it inside the API meant the build lived and died
with one API copy, and competed with students' chats for the same process. Here
it runs in its own process instead (course topic 5, message queues):

  1. the API stores the build in Postgres and adds a job to the "builds" queue;
  2. a worker takes the job (each job goes to exactly one worker), runs the
     builder graph, and writes progress events to Postgres;
  3. it acknowledges the job only when the build has reached review or failed.

A worker that dies mid-build never acknowledges, so after QUEUE_CLAIM_IDLE_MS
another worker takes the job over and starts the build again (up to 3 attempts).
Each worker runs one build at a time; run more workers for more builds at once.

The API can also run this loop inside its own process (API_RUN_WORKER=1, the
default), so `python -m engine.api` alone still builds bots on a laptop.
"""

import asyncio
import logging
import os
import socket

import redis

import engine.db  # noqa: F401  (first: on Windows it switches to the event loop psycopg needs)
from engine import shared
from engine.builds import QUEUE, BuildStore, run_build

log = logging.getLogger("worker")


async def _keep_alive(job_id: str, name: str) -> None:
    """While a build runs, tell the queue now and then that this worker is still on it."""
    while True:
        await asyncio.sleep(shared.CLAIM_IDLE_MS / 3000)
        try:
            await shared.touch_job(QUEUE, name, job_id)
        except redis.RedisError as e:
            log.warning("could not touch job %s: %s", job_id, e)


async def work(store: BuildStore, make_graph, name: str, stop: asyncio.Event | None = None) -> None:
    """Take jobs from the queue and run them, one at a time, until stop is set."""
    log.info("worker %s waiting for builds", name)
    while not (stop and stop.is_set()):
        try:
            job = await shared.next_job(QUEUE, name, block_ms=1000)
        except redis.RedisError as e:
            log.error("queue unavailable: %s", e)
            await asyncio.sleep(2)
            continue
        if job is None:
            continue
        job_id, fields = job
        log.info("build %s: started (job %s)", fields.get("build"), job_id)
        alive = asyncio.create_task(_keep_alive(job_id, name))
        try:
            await run_build(store, make_graph, fields["build"], name)
        finally:
            alive.cancel()
        await shared.ack_job(QUEUE, job_id)  # done: the queue forgets it
        log.info("build %s: done", fields.get("build"))


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def main() -> None:
    from engine.db import open_pool, postgres_checkpointer  # noqa: F811
    from engine.llm import get_llm
    from engine.step10_builder import BUILDER_MODEL, build_builder

    pool = await open_pool()
    try:
        async with postgres_checkpointer() as checkpointer:
            # The builder pauses for review in the shared checkpointer, so whichever
            # API copy receives the admin's decision can resume it.
            await work(BuildStore(pool), lambda: build_builder(get_llm(model=BUILDER_MODEL), checkpointer),
                       worker_name())
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
