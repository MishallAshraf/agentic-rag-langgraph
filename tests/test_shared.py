"""Lampus Phase 1, Step 7 - Shared state in Redis (course topics 8 and 3).

    python -m unittest tests.test_shared -v        # needs Redis (REDIS_URL)

Every API copy reads and writes the same Redis, so these checks stand in for
"two copies at once": many concurrent requests against one bucket, one lock,
one cache.
"""

import asyncio
import secrets
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests._env  # noqa: E402, F401  (unique REDIS_PREFIX)

from engine import shared  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TokenBucketTest(unittest.TestCase):
    def setUp(self):
        self.limiter = shared.RateLimiter()
        self.student = f"student:{secrets.token_hex(3)}"
        self.bot = f"bot:{secrets.token_hex(3)}"

    def test_burst_then_refused(self):
        async def go():
            return [await self.limiter.take({self.student: (10, 10)}) for _ in range(11)]
        waits = run(go())
        self.assertEqual(waits[:10], [0] * 10)
        self.assertGreaterEqual(waits[10], 1)  # 10 per minute: the next token in about 6 s
        self.assertLessEqual(waits[10], 7)

    def test_concurrent_requests_cannot_overdraw(self):
        async def go():  # 50 requests at the same moment for 10 tokens
            return await asyncio.gather(*(self.limiter.take({self.student: (10, 10)}) for _ in range(50)))
        self.assertEqual(run(go()).count(0), 10)

    def test_empty_bot_bucket_refuses_without_charging_the_student(self):
        async def go():
            for _ in range(2):
                await self.limiter.take({self.bot: (2, 2)})  # the school uses up its bucket
            refused = await self.limiter.take({self.student: (1, 1), self.bot: (2, 2)})
            own = await self.limiter.take({self.student: (1, 1)})  # the student's token is still there
            return refused, own
        refused, own = run(go())
        self.assertGreater(refused, 0)
        self.assertEqual(own, 0)


class FailureCounterTest(unittest.TestCase):
    def test_blocked_after_limit(self):
        counter, key = shared.FailureCounter(3, 60), f"1.2.3.4|{secrets.token_hex(3)}"

        async def go():
            before = await counter.retry_after(key)
            for _ in range(3):
                await counter.failed(key)
            return before, await counter.retry_after(key)
        before, after = run(go())
        self.assertEqual(before, 0)
        self.assertTrue(1 <= after <= 60)


class LockTest(unittest.TestCase):
    def test_one_holder_at_a_time(self):
        name = f"thread:test:{secrets.token_hex(4)}"

        async def go():
            first = await shared.acquire_lock(name, 30)
            second = await shared.acquire_lock(name, 30)
            await shared.release_lock(name, "not-the-token")  # a stranger can't free it
            still = await shared.is_locked(name)
            await shared.release_lock(name, first)
            third = await shared.acquire_lock(name, 30)
            await shared.release_lock(name, third)
            return first, second, still, third
        first, second, still, third = run(go())
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertTrue(still)
        self.assertIsNotNone(third)

    def test_many_racers_one_winner(self):
        name = f"thread:test:{secrets.token_hex(4)}"

        async def go():
            return await asyncio.gather(*(shared.acquire_lock(name, 30) for _ in range(20)))
        self.assertEqual(sum(t is not None for t in run(go())), 1)

    def test_lock_expires(self):
        name = f"thread:test:{secrets.token_hex(4)}"

        async def go():
            await shared.acquire_lock(name, 0.2)  # a copy that crashed mid-turn
            await asyncio.sleep(0.4)
            return await shared.acquire_lock(name, 30)
        self.assertIsNotNone(run(go()))


class CacheTest(unittest.TestCase):
    def test_shared_and_invalidated_per_collection(self):
        a, b = f"col_a_{secrets.token_hex(3)}", f"col_b_{secrets.token_hex(3)}"
        shared.cache_put(a, "what is x", "answer A")
        shared.cache_put(b, "what is x", "answer B")
        self.assertEqual(shared.cache_get(a, "what is x"), "answer A")
        shared.invalidate(a)  # a's documents were re-ingested
        self.assertIsNone(shared.cache_get(a, "what is x"))
        self.assertEqual(shared.cache_get(b, "what is x"), "answer B")


if __name__ == "__main__":
    unittest.main()
