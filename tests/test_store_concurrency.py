"""Lampus Phase 1, Step 3 - The auth store under concurrent requests (course topics 4 and 6).

    python -m unittest tests.test_store_concurrency -v

The SQLite store shared one connection between all requests, so concurrent
writes collided ("cannot start a transaction within a transaction") and a
check-then-add quota could be overrun. These tests fire many requests at the
same moment and check the two promises the Postgres store makes:
  - the audit chain stays intact, with every event recorded exactly once;
  - a daily quota of N turns lets exactly N turns through, however many race for it.
"""

import asyncio
import secrets
import sys
import unittest
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests._env  # noqa: E402, F401  (throwaway schema; QUOTA_TURNS_PER_DAY=3)

from engine import auth  # noqa: E402
from engine.auth import AuthStore  # noqa: E402

PASSWORD = "concurrency-test-password"


class ConcurrentStore(unittest.IsolatedAsyncioTestCase):
    """Its own schema: test_auth deliberately breaks the audit chain in the shared one."""

    async def asyncSetUp(self):
        self.schema = f"test_conc_{secrets.token_hex(4)}"
        with psycopg.connect(tests._env._BASE_URL, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {self.schema}")
        self.store = await AuthStore(make_conninfo(tests._env._BASE_URL,
                                                   options=f"-c search_path={self.schema}")).open()

    async def asyncTearDown(self):
        await self.store.close()
        with psycopg.connect(tests._env._BASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {self.schema} CASCADE")

    async def test_concurrent_audit_writes_keep_one_intact_chain(self):
        await asyncio.gather(*(self.store.audit("racer", "test.concurrent", detail={"n": i}) for i in range(50)))
        intact, rows, broken_at = await self.store.verify_audit()
        self.assertTrue(intact, f"chain broken at row {broken_at}")
        self.assertEqual(rows, 50)

    async def test_parallel_turns_cannot_overrun_the_quota(self):
        await self.store.create_user("quota_racer", PASSWORD)
        results = await asyncio.gather(*(self.store.reserve_turn("quota_racer") for _ in range(20)))
        granted = [r for r in results if r is None]
        self.assertEqual(len(granted), auth.QUOTA_TURNS)  # 3, not "3 plus whoever raced"
        self.assertEqual((await self.store.usage_today("quota_racer"))["turns"], auth.QUOTA_TURNS)
        self.assertTrue(all("Daily limit" in r for r in results if r is not None))


if __name__ == "__main__":
    unittest.main()
