"""Step 13 - Tests for authentication, authorization, quotas and the audit log.

    python -m unittest tests.test_auth -v

Runs against a throwaway user database and never calls the LLM: every request
here is answered (or refused) before a model would be involved.
"""

import asyncio
import os
import secrets
import sys
import unittest
from pathlib import Path

# Settings are read when the modules load, so set them first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests._env  # noqa: E402, F401

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engine import api  # noqa: E402
from engine.auth import AuthStore, today  # noqa: E402

PASSWORDS = {name: secrets.token_urlsafe(16) for name in ("alice", "bob", "root")}


async def make_users():
    store = await AuthStore().open()
    await store.create_user("alice", PASSWORDS["alice"], "member")
    await store.create_user("bob", PASSWORDS["bob"], "member")
    await store.create_user("root", PASSWORDS["root"], "admin")
    await store.grant("alice", "architecture_guide")
    await store.grant("bob", "architecture_guide")
    await store.grant("bob", "study_assistant")
    await store.close()


def db():
    """A direct connection to the test schema, for checks the API doesn't expose."""
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


class As:
    """One running app, many users: each request carries only this user's session cookie.

    (Several TestClients would each run the app's startup and shutdown, and the
    first to finish would close the shared database for the others.)
    """

    def __init__(self, client: TestClient, token: str | None = None):
        self.client, self.token = client, token

    def _cookies(self):
        self.client.cookies.clear()
        if self.token:
            self.client.cookies.set("session", self.token)

    def get(self, path, **kw):
        self._cookies()
        return self.client.get(path, **kw)

    def post(self, path, **kw):
        self._cookies()
        r = self.client.post(path, **kw)
        if path == "/auth/login" and r.status_code == 200:
            self.token = r.cookies.get("session")
        return r


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(make_users())
        cls.client = TestClient(api.app).__enter__()  # runs the app's startup once
        cls.anon = As(cls.client)
        cls.alice = cls.signed_in("alice")
        cls.bob = cls.signed_in("bob")
        cls.root = cls.signed_in("root")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @classmethod
    def signed_in(cls, name) -> As:
        user = As(cls.client)
        r = user.post("/auth/login", json={"username": name, "password": PASSWORDS[name]})
        assert r.status_code == 200, r.text
        return user

    def last_action(self, action):
        with db() as conn:
            return conn.execute("SELECT actor, bot_id, thread_id, detail FROM audit WHERE action = %s "
                                "ORDER BY id DESC LIMIT 1", (action,)).fetchone()

    # ------------------------------------------------------------ authentication

    def test_health_is_public_everything_else_needs_a_session(self):
        self.assertEqual(self.anon.get("/health").status_code, 200)
        for path in ("/bots", "/auth/me", "/bots/architecture_guide", "/admin/audit"):
            self.assertEqual(self.anon.get(path).status_code, 401, path)

    def test_wrong_password_and_unknown_user_look_the_same(self):
        c = As(self.client)
        a = c.post("/auth/login", json={"username": "alice", "password": "wrong-password-1"})
        b = c.post("/auth/login", json={"username": "nobody_here", "password": "wrong-password-1"})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
        self.assertEqual(a.status_code, 401)
        self.assertEqual(self.last_action("auth.login_failed")[0], "nobody_here")

    def test_session_cookie_is_httponly_and_samesite_strict(self):
        r = As(self.client).post("/auth/login", json={"username": "alice", "password": PASSWORDS["alice"]})
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        with db() as conn:  # only a hash of the token is stored
            stored = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
        token = cookie.split("session=")[1].split(";")[0]
        self.assertNotIn(token, stored)

    def test_repeated_failures_are_blocked(self):
        c = As(self.client)
        codes = [c.post("/auth/login", json={"username": "bob", "password": "not-the-password"}).status_code
                 for _ in range(api.LOGIN_LIMIT + 1)]
        self.assertEqual(codes[:-1], [401] * api.LOGIN_LIMIT)
        self.assertEqual(codes[-1], 429)

    def test_logout_ends_the_session_on_the_server(self):
        c = self.signed_in("alice")
        self.assertEqual(c.get("/auth/me").status_code, 200)
        self.assertEqual(c.post("/auth/logout").status_code, 204)
        # The same token, replayed after logout, no longer works.
        self.assertEqual(c.get("/auth/me").status_code, 401)

    def test_disabling_a_user_ends_their_sessions(self):
        asyncio.run(self._store_call("create_user", "carol", PASSWORDS["alice"], "member"))
        c = As(self.client)
        c.post("/auth/login", json={"username": "carol", "password": PASSWORDS["alice"]})
        self.assertEqual(c.get("/auth/me").status_code, 200)
        asyncio.run(self._store_call("set_disabled", "carol", True))
        self.assertEqual(c.get("/auth/me").status_code, 401)

    async def _store_call(self, method, *args):
        store = await AuthStore().open()
        try:
            await getattr(store, method)(*args)
        finally:
            await store.close()

    # ------------------------------------------------------------ authorization

    def test_members_see_only_granted_bots_admins_see_all(self):
        ids = lambda c: sorted(b["id"] for b in c.get("/bots").json())  # noqa: E731
        self.assertEqual(ids(self.alice), ["architecture_guide"])
        self.assertEqual(ids(self.bob), ["architecture_guide", "study_assistant"])
        self.assertEqual(ids(self.root), sorted(api.app.state.platform.bots))  # every bot

    def test_an_ungranted_bot_looks_like_a_missing_one(self):
        hidden = self.alice.get("/bots/study_assistant")
        missing = self.alice.get("/bots/no_such_bot")
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.alice.post("/bots/study_assistant/threads").status_code, 404)
        actor, bot, _, _ = self.last_action("access.denied")
        self.assertEqual((actor, bot), ("alice", "study_assistant"))

    def test_conversations_are_private(self):
        thread = self.alice.post("/bots/architecture_guide/threads").json()["thread_id"]
        self.assertEqual(self.alice.get(f"/bots/architecture_guide/threads/{thread}").status_code, 200)
        base = f"/bots/architecture_guide/threads/{thread}"
        self.assertEqual(self.bob.get(base).status_code, 404)
        self.assertEqual(self.bob.post(f"{base}/messages", json={"text": "hi"}).status_code, 404)
        self.assertEqual(self.bob.post(f"{base}/approval", json={"decision": "approve"}).status_code, 404)
        self.assertEqual(self.last_action("access.denied")[2], thread)
        # Bob's own list does not include it; Alice's does.
        self.assertNotIn(thread, [t["thread_id"] for t in self.bob.get("/bots/architecture_guide/threads").json()])
        self.assertIn(thread, [t["thread_id"] for t in self.alice.get("/bots/architecture_guide/threads").json()])

    def test_admin_routes_are_for_admins(self):
        self.assertEqual(self.alice.get("/admin/audit").status_code, 403)
        r = self.root.get("/admin/audit?limit=5")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["chain"]["intact"])

    def test_notes_folders_are_per_user(self):
        from engine.tool_registry import notes_dir
        bot = api.app.state.platform.bots["study_assistant"].config
        self.assertNotEqual(notes_dir(bot, "alice"), notes_dir(bot, "bob"))
        self.assertEqual(notes_dir(bot, "alice").parent, notes_dir(bot))

    # ------------------------------------------------------------ quotas

    def test_daily_turn_quota_is_enforced_before_any_model_call(self):
        thread = self.bob.post("/bots/architecture_guide/threads").json()["thread_id"]
        with db() as conn:
            conn.execute("INSERT INTO usage VALUES ('bob', %s, 3, 0) ON CONFLICT (username, day) "
                         "DO UPDATE SET turns = 3, tokens = 0", (today(),))
        r = self.bob.post(f"/bots/architecture_guide/threads/{thread}/messages", json={"text": "hello"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Daily limit", r.json()["detail"])
        self.assertEqual(self.last_action("quota.exceeded")[0], "bob")
        self.assertEqual(self.bob.get("/auth/me").json()["usage"]["turns"], 3)

    # ------------------------------------------------------------ audit log (last: it edits the log)

    def test_z_editing_the_audit_log_is_detected(self):
        ok = self.root.get("/admin/audit").json()["chain"]
        self.assertTrue(ok["intact"])
        with db() as conn:  # rewrite history: pretend a refusal never happened
            conn.execute("UPDATE audit SET actor = 'someone_else' WHERE action = 'access.denied'")
        broken = self.root.get("/admin/audit").json()["chain"]
        self.assertFalse(broken["intact"])
        self.assertIsNotNone(broken["broken_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
