"""State every API copy must share: rate limits, conversation locks and caches, in Redis.

    REDIS_URL      redis://host:6379/0   (default: the redis service in docker-compose.yml)
    REDIS_PREFIX   prefix for every key  (default "lampus:"; tests use their own)

Until Lampus Phase 1, Step 7 these lived in each process's memory. That is fine
for one process, but with several API copies behind a load balancer (Step 9)
each copy kept its own count and its own locks: a student limited to 10
messages a minute could send 10 to each copy, and two copies could run turns
in the same conversation at once (loadtest/bench_shared_state.py).

Redis is one small, fast in-memory server that all copies talk to. Each check
below is a single Redis command or a short Lua script, which Redis runs
start to finish without interleaving anyone else's commands, so two copies can
never both "see 1 token left" and both take it (course topics 8 and 3).
"""

import logging
import os
import secrets
import threading

import redis
import redis.asyncio as aioredis

log = logging.getLogger("shared")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PREFIX = os.getenv("REDIS_PREFIX", "lampus:")

_lock = threading.Lock()
_async: dict[int, aioredis.Redis] = {}  # one client per event loop (tests start several)
_sync: redis.Redis | None = None


def async_client() -> aioredis.Redis:
    import asyncio

    loop_id = id(asyncio.get_running_loop())
    with _lock:
        if loop_id not in _async:
            _async[loop_id] = aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    return _async[loop_id]


def sync_client() -> redis.Redis:
    """For code running in worker threads (retrieval and the search cache)."""
    global _sync
    with _lock:
        if _sync is None:
            _sync = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    return _sync


# ------------------------------------------------------------------ rate limits (topic 8)

# A token bucket per key. Each bucket holds up to `capacity` tokens and refills at
# `rate` tokens per second; a request takes one token from EVERY bucket it names,
# or from none if any is empty. Redis's own clock (TIME) is used, so copies on
# machines whose clocks differ still agree.
TOKEN_BUCKET = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local wait, tokens = 0, {}
for i, key in ipairs(KEYS) do
  local capacity, rate = tonumber(ARGV[2 * i - 1]), tonumber(ARGV[2 * i])
  local b = redis.call('HMGET', key, 'tokens', 'ts')
  local level = tonumber(b[1]) or capacity
  local ts = tonumber(b[2]) or now
  level = math.min(capacity, level + (now - ts) * rate)
  tokens[i] = level
  if level < 1 then wait = math.max(wait, (1 - level) / rate) end
end
if wait > 0 then return tostring(wait) end
for i, key in ipairs(KEYS) do
  local capacity, rate = tonumber(ARGV[2 * i - 1]), tonumber(ARGV[2 * i])
  redis.call('HSET', key, 'tokens', tokens[i] - 1, 'ts', now)
  redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000) + 1000)
end
return '0'
"""


class RateLimiter:
    """Token buckets in Redis. take() returns 0 if the request may go ahead, else seconds to wait."""

    def __init__(self):
        self._script = None

    async def take(self, buckets: dict[str, tuple[float, float]]) -> int:
        """buckets: {name: (capacity, per_minute)}, e.g. {"student:ana": (10, 10)}."""
        client = async_client()
        if self._script is None:
            self._script = client.register_script(TOKEN_BUCKET)
        keys = [f"{PREFIX}rl:{name}" for name in buckets]
        args = [x for capacity, per_minute in buckets.values() for x in (capacity, per_minute / 60)]
        try:
            wait = float(await self._script(keys=keys, args=args, client=client))
        except redis.RedisError as e:
            # Fail open: a limiter outage must not take the tutor down. The daily
            # quota (Postgres) still caps each student.
            log.error("rate limiter unavailable, letting the request through: %s", e)
            return 0
        return 0 if wait == 0 else int(wait) + 1


class FailureCounter:
    """Counts failures per key in a fixed window (sign-in attempts): blocked after `limit`."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window_s = limit, window_s

    async def retry_after(self, key: str) -> int:
        client, name = async_client(), f"{PREFIX}fail:{key}"
        try:
            count = int(await client.get(name) or 0)
            return max(1, await client.ttl(name)) if count >= self.limit else 0
        except redis.RedisError as e:
            log.error("failure counter unavailable: %s", e)
            return 0

    async def failed(self, key: str) -> None:
        client, name = async_client(), f"{PREFIX}fail:{key}"
        try:
            async with client.pipeline(transaction=True) as pipe:
                await pipe.incr(name).expire(name, self.window_s, nx=True).execute()
        except redis.RedisError as e:
            log.error("failure counter unavailable: %s", e)


# ------------------------------------------------------------------ locks

# Delete the lock only if it still holds our token: if our turn outlived the
# lock's expiry and someone else took it, we must not release THEIR lock.
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


async def acquire_lock(name: str, ttl_s: float) -> str | None:
    """Take a lock shared by all copies; returns a token to release it with, or None if taken.

    The lock expires by itself after ttl_s, so a copy that crashes mid-turn can't
    block the conversation forever.
    """
    token = secrets.token_hex(16)
    ok = await async_client().set(f"{PREFIX}lock:{name}", token, nx=True, px=int(ttl_s * 1000))
    return token if ok else None


async def release_lock(name: str, token: str) -> None:
    client = async_client()
    await client.eval(RELEASE, 1, f"{PREFIX}lock:{name}", token)


async def is_locked(name: str) -> bool:
    return bool(await async_client().exists(f"{PREFIX}lock:{name}"))


# ------------------------------------------------------------------ cache (topic 3)

CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", str(24 * 3600)))


def _generation(client, collection: str) -> str:
    return client.get(f"{PREFIX}gen:{collection}") or "0"


def cache_get(collection: str, key: str) -> str | None:
    """A cached value for this collection, or None (also when Redis is down)."""
    try:
        client = sync_client()
        return client.get(f"{PREFIX}cache:{collection}:{_generation(client, collection)}:{key}")
    except redis.RedisError as e:
        log.warning("cache unavailable: %s", e)
        return None


def cache_put(collection: str, key: str, value: str) -> None:
    try:
        client = sync_client()
        client.set(f"{PREFIX}cache:{collection}:{_generation(client, collection)}:{key}", value, ex=CACHE_TTL_S)
    except redis.RedisError as e:
        log.warning("cache unavailable: %s", e)


def invalidate(collection: str) -> None:
    """After a collection's documents change: every copy stops using older cached results.

    Bumping a generation number is one write, instead of finding and deleting
    every old key; the old entries simply expire.
    """
    try:
        sync_client().incr(f"{PREFIX}gen:{collection}")
    except redis.RedisError as e:
        log.error("could not invalidate the cache for %s: %s", collection, e)


# ------------------------------------------------------------------ queue and pub/sub (topic 5)

# A job queue on a Redis Stream with a consumer group. XADD appends a job; each
# worker reads with XREADGROUP, so a job goes to exactly one worker, and stays
# "pending" until the worker acknowledges it (XACK) when the job is done. If a
# worker dies, its pending jobs sit idle; another worker takes them over with
# XAUTOCLAIM after CLAIM_IDLE_MS. A long job "touches" its entry now and then so
# it isn't taken over while still running (at-least-once delivery: a job may run
# twice, so running it must be safe to repeat).
GROUP = "workers"
CLAIM_IDLE_MS = int(os.getenv("QUEUE_CLAIM_IDLE_MS", "60000"))


def stream_key(queue: str) -> str:
    return f"{PREFIX}queue:{queue}"


async def enqueue(queue: str, job: dict) -> str:
    return await async_client().xadd(stream_key(queue), job)


async def _ensure_group(client, key: str) -> None:
    try:
        await client.xgroup_create(key, GROUP, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):  # the group exists already: fine
            raise


async def next_job(queue: str, consumer: str, block_ms: int = 5000) -> tuple[str, dict] | None:
    """The next job for this worker: first any job a dead worker left behind, then a new one."""
    client, key = async_client(), stream_key(queue)
    await _ensure_group(client, key)
    _, claimed, _ = await client.xautoclaim(key, GROUP, consumer, min_idle_time=CLAIM_IDLE_MS, count=1)
    if claimed:
        return claimed[0]
    got = await client.xreadgroup(GROUP, consumer, {key: ">"}, count=1, block=block_ms)
    return got[0][1][0] if got else None


async def touch_job(queue: str, consumer: str, job_id: str) -> None:
    """Reset the job's idle time: 'still working on it', so no one takes it over."""
    await async_client().xclaim(stream_key(queue), GROUP, consumer, 0, [job_id], justid=True)


async def ack_job(queue: str, job_id: str) -> None:
    await async_client().xack(stream_key(queue), GROUP, job_id)


async def publish(channel: str, message: dict) -> None:
    """Pub/sub: deliver to every process subscribed right now; nobody listening, nobody gets it.
    So it only says "something changed"; what changed is read from Postgres."""
    import json

    try:
        await async_client().publish(f"{PREFIX}{channel}", json.dumps(message))
    except redis.RedisError as e:  # listeners also re-check on a timer, so a lost message only delays them
        log.warning("could not publish on %s: %s", channel, e)


async def subscribe(channel: str):
    """A listener on one channel (its own connection), ready once this returns.

    It waits for Redis to confirm the subscription, so a message published right
    after this returns is not missed. Read with get_message(timeout=...); close
    with aclose().
    """
    listener = async_client().pubsub()
    await listener.subscribe(f"{PREFIX}{channel}")
    for _ in range(50):
        confirm = await listener.get_message(timeout=0.1)
        if confirm and confirm["type"] == "subscribe":
            return listener
    await listener.aclose()
    raise redis.RedisError(f"no subscription confirmation for {channel}")
