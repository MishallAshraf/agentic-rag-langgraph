"""Lampus Phase 1, Step 10 - Signed webhooks to a school's own systems (course topic 9).

    python -m engine.webhooks secret        # print a new signing secret for WEBHOOK_SECRET

A webhook is an HTTP POST that Lampus sends to a school's server when something
happens, so the school doesn't have to keep asking. Events (ids only, never
message text):

  build.finished          a bot build reached review or failed
  build.decided           an admin approved or rejected a build
  conversation.flagged    the safety check routed a student's message as a crisis

Anyone on the internet can POST to the school's endpoint, so each message is
signed, following the Standard Webhooks spec (https://www.standardwebhooks.com,
v1.0.0):

  webhook-id          msg_<random>; the same on every retry of the same event
  webhook-timestamp   Unix seconds when it was sent
  webhook-signature   "v1," + base64(HMAC-SHA256(secret, "<id>.<timestamp>.<body>"))

The receiver recomputes the signature with the shared secret (verify() below),
rejects messages older than 5 minutes (a recorded message can't be replayed
later) and ignores ids it has already handled (a retry or a replay within the
5 minutes).

Delivery goes through the Redis queue from Step 8, so a slow or broken school
server never slows a student's turn. A worker sends each message and retries
failures on the spec's schedule (5 s, 5 min, 30 min, 2 h, 5 h), then gives up.
If the worker dies, another takes the job over and starts the schedule again:
delivery is at least once, which is why receivers drop repeated ids.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
import redis

from engine import shared

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")        # the school's endpoint; empty: no webhooks
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # whsec_<base64>, from `python -m engine.webhooks secret`
QUEUE = "webhooks"
RETRY_AFTER_S = [5, 300, 1800, 7200, 18000]       # after the first attempt (Standard Webhooks' schedule)
TOLERANCE_S = 300                                 # receivers: reject messages older or newer than this
TIMEOUT_S = 15
MAX_SENDING = 20                                  # deliveries in progress at once per worker

log = logging.getLogger("webhooks")


def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def _key(secret: str) -> bytes:
    return base64.b64decode(secret.removeprefix("whsec_"))


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(_key(secret), signed, hashlib.sha256).digest()).decode()


def verify(secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    """For receivers: True only if the message was signed with this secret, unchanged, and is fresh.

    `headers` with lowercase names. Checking that an id wasn't seen before is the
    receiver's job (it needs storage that outlives one request).
    """
    try:
        msg_id, ts = headers["webhook-id"], int(headers["webhook-timestamp"])
        signatures = headers["webhook-signature"].split()
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > TOLERANCE_S:
        return False
    expected = sign(secret, msg_id, ts, body)
    # compare_digest takes the same time however many characters match, so the
    # signature can't be guessed one character at a time.
    return any(hmac.compare_digest(expected, s) for s in signatures if s.startswith("v1,"))


async def emit(event_type: str, data: dict) -> None:
    """Queue one event for delivery. Never raises: a webhook must not break what triggered it."""
    if not WEBHOOK_URL:
        return
    message = {"id": f"msg_{uuid.uuid4().hex}", "type": event_type,
               "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data": data}
    try:
        await shared.enqueue(QUEUE, {"message": json.dumps(message)})
    except redis.RedisError as e:
        log.error("could not queue webhook %s: %s", event_type, e)


async def send_once(client: httpx.AsyncClient, url: str, secret: str, message: dict) -> int | None:
    """One delivery attempt; the HTTP status, or None if the server couldn't be reached."""
    body = json.dumps({"type": message["type"], "timestamp": message["timestamp"], "data": message["data"]}).encode()
    ts = int(time.time())  # signed at each attempt, so a late retry is still fresh
    headers = {"content-type": "application/json", "webhook-id": message["id"], "webhook-timestamp": str(ts),
               "webhook-signature": sign(secret, message["id"], ts, body)}
    try:
        return (await client.post(url, content=body, headers=headers)).status_code
    except httpx.HTTPError:
        return None


async def deliver(client: httpx.AsyncClient, message: dict, url: str = "", secret: str = "",
                  retry_after: list[float] | None = None) -> bool:
    """Send until a 2xx, retrying on the schedule; False if it never arrived."""
    url, secret = url or WEBHOOK_URL, secret or WEBHOOK_SECRET
    waits = RETRY_AFTER_S if retry_after is None else retry_after
    for attempt in range(len(waits) + 1):
        status = await send_once(client, url, secret, message)
        if status is not None and 200 <= status < 300:
            return True
        if status == 410:  # the school removed the endpoint: stop sending
            break
        if attempt < len(waits):
            await asyncio.sleep(waits[attempt] * random.uniform(0.9, 1.1))  # jitter: don't retry in lockstep
    log.error("webhook %s (%s) not delivered after %d attempts", message["id"], message["type"], attempt + 1)
    return False


async def deliver_loop(name: str, stop: asyncio.Event | None = None) -> None:
    """Take queued webhooks and deliver them, several at once, until stop is set."""
    if not WEBHOOK_URL:
        return
    sending = asyncio.Semaphore(MAX_SENDING)
    running: set[asyncio.Task] = set()

    async def one(job_id: str, message: dict):
        async def keep_alive():  # tell the queue this worker is still on it, through long retry waits
            while True:
                await asyncio.sleep(shared.CLAIM_IDLE_MS / 3000)
                try:
                    await shared.touch_job(QUEUE, name, job_id)
                except redis.RedisError:
                    pass
        alive = asyncio.create_task(keep_alive())
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                await deliver(client, message)
            await shared.ack_job(QUEUE, job_id)  # delivered or given up: the queue forgets it
        finally:
            alive.cancel()
            sending.release()

    while not (stop and stop.is_set()):
        await sending.acquire()
        try:
            job = await shared.next_job(QUEUE, name, block_ms=1000)
        except redis.RedisError as e:
            sending.release()
            log.error("webhook queue unavailable: %s", e)
            await asyncio.sleep(2)
            continue
        if job is None:
            sending.release()
            continue
        task = asyncio.create_task(one(job[0], json.loads(job[1]["message"])))
        running.add(task)
        task.add_done_callback(running.discard)
    for task in running:
        task.cancel()


if __name__ == "__main__":
    if sys.argv[1:] == ["secret"]:
        print(new_secret())
    else:
        print(__doc__)
