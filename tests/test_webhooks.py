"""Lampus Phase 1, Step 10 - Signed webhooks (course topic 9).

    python -m unittest tests.test_webhooks -v

No network and no Redis: signing and checking are plain functions, and the
delivery retries are tested against a fake school server (httpx's MockTransport).
"""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import webhooks  # noqa: E402

SECRET = webhooks.new_secret()
BODY = json.dumps({"type": "build.finished", "data": {"bot_id": "cafe"}}).encode()


def signed(body: bytes = BODY, secret: str = SECRET, msg_id: str = "msg_1", ts: int | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts),
            "webhook-signature": webhooks.sign(secret, msg_id, ts, body)}


class SignatureTest(unittest.TestCase):
    def test_genuine_message_passes(self):
        self.assertTrue(webhooks.verify(SECRET, signed(), BODY))

    def test_changed_body_fails(self):
        self.assertFalse(webhooks.verify(SECRET, signed(), BODY.replace(b"cafe", b"evil")))

    def test_other_secret_fails(self):
        self.assertFalse(webhooks.verify(SECRET, signed(secret=webhooks.new_secret()), BODY))

    def test_changed_id_or_timestamp_fails(self):
        headers = signed()
        self.assertFalse(webhooks.verify(SECRET, {**headers, "webhook-id": "msg_2"}, BODY))
        self.assertFalse(webhooks.verify(SECRET, {**headers, "webhook-timestamp": str(int(time.time()) - 1)}, BODY))

    def test_old_message_is_refused_even_if_signed(self):
        old = int(time.time()) - webhooks.TOLERANCE_S - 60  # a recorded message, replayed later
        self.assertFalse(webhooks.verify(SECRET, signed(ts=old), BODY))

    def test_missing_headers_and_rotated_secrets(self):
        self.assertFalse(webhooks.verify(SECRET, {}, BODY))
        # While a secret is being replaced, the sender signs with both; either may match.
        old_secret = webhooks.new_secret()
        both = signed(secret=old_secret)
        both["webhook-signature"] += " " + signed()["webhook-signature"]
        self.assertTrue(webhooks.verify(SECRET, both, BODY))
        self.assertTrue(webhooks.verify(old_secret, both, BODY))

    def test_signature_format(self):
        sig = signed()["webhook-signature"]
        self.assertTrue(sig.startswith("v1,"))
        self.assertTrue(SECRET.startswith("whsec_"))


class DeliveryTest(unittest.TestCase):
    def test_retried_until_delivered_with_the_same_id(self):
        seen = []

        def school(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            ok = webhooks.verify(SECRET, dict(request.headers), request.content)
            return httpx.Response(200 if ok and len(seen) >= 3 else 500)  # down for the first two tries
        message = {"id": "msg_retry", "type": "build.finished", "timestamp": "now", "data": {}}

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(school)) as client:
                return await webhooks.deliver(client, message, "https://school.test/hook", SECRET,
                                              retry_after=[0, 0, 0, 0])
        self.assertTrue(asyncio.run(go()))
        self.assertEqual(len(seen), 3)
        self.assertEqual({h["webhook-id"] for h in seen}, {"msg_retry"})  # receivers drop repeats by id

    def test_gives_up_on_410_gone(self):
        calls = []

        async def go():
            transport = httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(410))
            async with httpx.AsyncClient(transport=transport) as client:
                return await webhooks.deliver(client, {"id": "m", "type": "t", "timestamp": "now", "data": {}},
                                              "https://school.test/hook", SECRET, retry_after=[0, 0, 0])
        self.assertFalse(asyncio.run(go()))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
