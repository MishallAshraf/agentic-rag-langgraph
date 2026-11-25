"""The Postgres connection pool shared by the engine's stores.

    DATABASE_URL   postgresql://user:password@host:5432/dbname
                   (default: the database in docker-compose.yml)

A pool keeps a few connections open and lends one to each request for the
length of one unit of work, then takes it back. Requests therefore never share
a connection mid-transaction, which is what broke the SQLite store under load:
one connection for everyone, so one request's BEGIN landed inside another's
transaction (see loadtest/README.md, baseline).

On Windows, psycopg's async mode can't use the default event loop (Proactor),
so importing this module switches Windows to the Selector loop. Linux, where the
engine runs in production, is unaffected.

Two pools per API process: one for users, quotas and the audit log (engine/auth.py),
and one for conversation checkpoints (postgres_checkpointer below), so a burst of
checkpoint writes can't starve sign-ins.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://lampus:lampus@localhost:5432/lampus")
POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
SCHEMA_FILE = Path(__file__).parent / "store" / "schema.sql"


async def open_pool(url: str = DATABASE_URL) -> AsyncConnectionPool:
    """Open a pool and make sure the tables exist."""
    # prepare_threshold=None: never use server-side prepared statements. The schema
    # script has several statements (which a prepared statement can't hold), and it
    # keeps the pool safe behind a connection proxy such as PgBouncer.
    pool = AsyncConnectionPool(url, min_size=POOL_MIN, max_size=POOL_MAX, open=False,
                               kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None})
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        # Two processes starting at once must not both create the tables: serialise on a lock.
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('lampus.schema'))")
            await conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    return pool


@asynccontextmanager
async def postgres_checkpointer(url: str = DATABASE_URL):
    """LangGraph's Postgres checkpointer on its own pool: where conversations are saved.

    Every process sees the same conversations, so any API copy can continue any
    conversation (Phase 1, Step 9), and many conversations save at once instead of
    queueing for one SQLite connection.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    pool = await open_pool(url)
    try:
        saver = AsyncPostgresSaver(pool)
        # setup() creates and migrates LangGraph's tables. Hold a session lock on a
        # separate connection meanwhile, so two processes starting together don't race.
        # Wait for it by polling, never with a blocking pg_advisory_lock(): a waiting
        # statement keeps a transaction open, and setup()'s CREATE INDEX CONCURRENTLY
        # waits for every open transaction to end, so the two copies would wait for each
        # other forever (found starting two API copies at once, Phase 1 Step 7).
        async with pool.connection() as lock_conn:
            while not (await (await lock_conn.execute(
                    "SELECT pg_try_advisory_lock(hashtext('lampus.checkpoints')) AS ok")).fetchone())["ok"]:
                await asyncio.sleep(0.2)
            try:
                await saver.setup()
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock(hashtext('lampus.checkpoints'))")
        yield saver
    finally:
        await pool.close()

