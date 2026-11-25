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
"""

import asyncio
import os
import sys
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
    pool = AsyncConnectionPool(url, min_size=POOL_MIN, max_size=POOL_MAX, open=False,
                               kwargs={"row_factory": dict_row, "autocommit": True})
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        # Two processes starting at once must not both create the tables: serialise on a lock.
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('lampus.schema'))")
            await conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    return pool
