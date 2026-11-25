"""Lampus Phase 1, Step 10 - Idempotency keys: a resent request runs once (course topic 9).

A phone sends a message, the connection drops before the answer arrives, and
the app sends it again. Without help the server can't tell a resend from a new
message: the turn runs twice, the student is charged twice and the question is
in the conversation twice.

So the client puts a random Idempotency-Key header on each new request, and
reuses it when it resends. The first request with a key claims it here (a row in
Postgres; the primary key makes "first one wins" exact, even across API copies).
When the request finishes, its response is stored with the key. A request
arriving with a claimed key:

  - same request, finished:       gets the stored response back, nothing runs again
  - same request, still running:  409, try again shortly
  - different request, same key:  422, a client bug

A request that fails (an error, the client gone mid-turn) gives its key back, so
the resend runs normally. A running claim older than STALE_AFTER_S belongs to a
copy that crashed and can be taken over. Keys are kept for KEEP_HOURS.
"""

import hashlib
import json

from psycopg.types.json import Jsonb

KEEP_HOURS = 24


class KeyInUse(Exception):
    """The key belongs to a request that is still running."""


class KeyReused(Exception):
    """The key was used for a different request."""


def request_hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, pool, stale_after_s: float):
        self.pool = pool
        self.stale_after_s = stale_after_s

    async def claim(self, username: str, key: str, req_hash: str) -> dict | None:
        """None: this request owns the key, go ahead. A dict: the stored response to send back."""
        async with self.pool.connection() as conn:
            for _ in range(2):  # the row may vanish between the two statements (its request failed)
                if await (await conn.execute(
                        "INSERT INTO idempotency_keys (username, key, request_hash, status) "
                        "VALUES (%s, %s, %s, 'running') ON CONFLICT DO NOTHING RETURNING 1",
                        (username, key, req_hash))).fetchone():
                    return None
                row = await (await conn.execute(
                    "SELECT request_hash, status, response FROM idempotency_keys WHERE username = %s AND key = %s",
                    (username, key))).fetchone()
                if row is not None:
                    break
            else:
                raise KeyInUse()
            if row["request_hash"] != req_hash:
                raise KeyReused()
            if row["status"] == "done":
                return row["response"]
            # Still running. If for longer than any request may take, its copy died: take it over.
            if await (await conn.execute(
                    "UPDATE idempotency_keys SET created = now() WHERE username = %s AND key = %s "
                    "AND status = 'running' AND created < now() - make_interval(secs => %s) RETURNING 1",
                    (username, key, self.stale_after_s))).fetchone():
                return None
            raise KeyInUse()

    async def finish(self, username: str, key: str, response: dict) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE idempotency_keys SET status = 'done', response = %s "
                               "WHERE username = %s AND key = %s", (Jsonb(response), username, key))

    async def release(self, username: str, key: str) -> None:
        """The request failed: forget the key, so a resend runs."""
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM idempotency_keys WHERE username = %s AND key = %s AND status = 'running'",
                               (username, key))

    async def clean(self) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM idempotency_keys WHERE created < now() - make_interval(hours => %s)",
                                     (KEEP_HOURS,))
            return cur.rowcount
