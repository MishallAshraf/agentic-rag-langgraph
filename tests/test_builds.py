"""Step 15 - Tests for building bots through the API: uploads, progress, review, serving.

    python -m unittest tests.test_builds -v

A stand-in builder graph replaces Step 10's (same nodes, same review
interrupt, no LLM and no indexing), and bots are saved to a temporary folder,
so these tests check the upload rules, the job and the permissions for free.
Since Lampus Phase 1, Step 8 builds go through the Redis queue to a worker; here
the worker runs inside the API process (API_RUN_WORKER=1), so Redis is needed.
"""

import asyncio
import json
import secrets
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests._env  # noqa: E402, F401  (before api.py reads its settings)

import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from engine import api  # noqa: E402
from engine import builds  # noqa: E402
from engine import step10_builder  # noqa: E402
from engine.auth import AuthStore  # noqa: E402
from engine.bot_config import ROOT  # noqa: E402
from engine.step10_builder import BuildState, save_files  # noqa: E402


class As:
    """One running app, many users (as in test_auth: importing it would change DATABASE_URL)."""

    def __init__(self, client: TestClient):
        self.client, self.token = client, None

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


PASSWORDS = {name: secrets.token_urlsafe(16) for name in ("builder_member", "builder_admin")}
WORK = ROOT / "data" / "test_builds"   # inside the project, as bot folders must be (data/ is gitignored)
UPLOADS = WORK / "uploads"
GATE = threading.Event()                          # holds a build in "ingest" while set is pending
GATE.set()

MD = b"# Leave policy\n\nStaff get 25 days of paid leave a year.\n"
PDF = b"%PDF-1.4\n% a tiny stand-in\n"


def fake_builder():
    """Step 10's builder shape: ingest -> draft -> validate -> write_tests -> review (interrupt) -> save."""

    def ingest(state: BuildState):
        GATE.wait(10)
        files = [p.name for p in (ROOT / state["folder"]).iterdir()]
        return {"log": [f"ingested {len(files)} files as {len(files) * 3} chunks into bot_{state['bot_id']}"]}

    def draft(state: BuildState):
        return {"spec": {"name": "Handbook Helper"}}

    def validate(state: BuildState):
        if "FAIL" in state["use_case"]:
            return Command(goto=END, update={"problems": ["suggested question 'x' is not answered"]})
        base = yaml.safe_load((ROOT / "bots" / "architecture_guide.yaml").read_text(encoding="utf-8"))
        config = {**base, "id": state["bot_id"], "name": "Handbook Helper",
                  "knowledge": {"folder": state["folder"], "collection": f"bot_{state['bot_id']}",
                                "subject": "the staff handbook"}}
        return Command(goto="write_tests", update={"config": config, "problems": []})

    def write_tests(state: BuildState):
        return {"tests": [{"id": "gen-1", "turns": ["How much leave do I get?"]}], "log": ["wrote 1 tests"]}

    def review(state: BuildState):
        decision = interrupt({"config": state["config"], "tests": state["tests"], "log": state["log"]})
        return Command(goto="save" if decision == "approve" else END)

    def save(state: BuildState):
        return {"saved": save_files(state["config"], state["tests"])}

    g = StateGraph(BuildState)
    for name, fn in [("ingest", ingest), ("draft", draft), ("write_tests", write_tests), ("save", save)]:
        g.add_node(name, fn)
    g.add_node("validate", validate, destinations=("write_tests", END))
    g.add_node("review", review, destinations=("save", END))
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "draft")
    g.add_edge("draft", "validate")
    g.add_edge("write_tests", "review")
    g.add_edge("save", END)
    # One saver for every graph made: the worker pauses the build and a fresh graph
    # resumes it at the decision, as the shared Postgres checkpointer allows in the app.
    return g.compile(checkpointer=CHECKPOINTER)


CHECKPOINTER = InMemorySaver()


async def make_users():
    store = await AuthStore().open()
    await store.create_user("builder_member", PASSWORDS["builder_member"], "member")
    await store.create_user("builder_admin", PASSWORDS["builder_admin"], "admin")
    await store.close()


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(make_users())
        cls.client = TestClient(api.app).__enter__()
        # Saved bots and their tests go to a temporary folder, never into the project's bots/.
        step10_builder.BOTS_DIR = api.BOTS_DIR = WORK / "bots"
        step10_builder.BOT_EVALS_DIR = WORK / "evals"
        builds.UPLOADS_DIR = UPLOADS
        api.app.state.builds.make_graph = fake_builder
        cls.anon = As(cls.client)
        cls.alice, cls.root = cls.signed_in("builder_member"), cls.signed_in("builder_admin")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        shutil.rmtree(WORK, ignore_errors=True)
        step10_builder.BOTS_DIR = api.BOTS_DIR = ROOT / "bots"
        step10_builder.BOT_EVALS_DIR = ROOT / "evals" / "bots"
        builds.UPLOADS_DIR = ROOT / "documents" / "uploads"

    @classmethod
    def signed_in(cls, name) -> As:
        user = As(cls.client)
        assert user.post("/auth/login", json={"username": name, "password": PASSWORDS[name]}).status_code == 200
        return user

    # ---------------------------------------------------------------- helpers

    def start(self, bot_id, files=(("handbook.md", MD),), use_case="Answer staff questions about the handbook.",
              who=None):
        return (who or self.root).post("/admin/builds", data={"bot_id": bot_id, "use_case": use_case},
                                       files=[("files", f) for f in files])

    def wait(self, build_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            build = self.root.get(f"/admin/builds/{build_id}").json()
            if build["status"] not in ("queued", "running"):
                return build
            time.sleep(0.05)
        self.fail("the build did not finish")

    def events(self, build_id, headers=None):
        r = self.root.get(f"/admin/builds/{build_id}/events", headers=headers or {})
        data = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
        self.assertEqual(data[-1], {"status": "review"})  # the stream ends by saying where the build stopped
        return data[:-1]

    # ---------------------------------------------------------------- tests

    def test_only_admins_can_build(self):
        self.assertEqual(self.start("x_bot", who=self.anon).status_code, 401)
        self.assertEqual(self.start("x_bot", who=self.alice).status_code, 403)
        self.assertEqual(self.alice.get("/admin/builds").status_code, 403)
        self.assertFalse((UPLOADS / "x_bot").exists())  # refused before the upload was stored

    def test_uploads_are_checked_before_anything_is_stored(self):
        cases = {
            "only .pdf, .docx and .md": dict(files=(("run.exe", b"MZ..."),)),
            "not a real .pdf": dict(files=(("report.pdf", b"<html>not a pdf</html>"),)),
            "UTF-8": dict(files=(("notes.md", b"\xff\xfe\x00bad"),)),
            "empty": dict(files=(("empty.md", b""),)),
            "already exists": dict(bot_id="architecture_guide"),
            "lowercase letters": dict(bot_id="Bad Id!"),
            "use case": dict(use_case="too short"),
        }
        for message, kw in cases.items():
            with self.subTest(message):
                r = self.start(kw.pop("bot_id", "checked_bot"), **kw)
                self.assertEqual(r.status_code, 400)
                self.assertIn(message, r.json()["detail"])
        self.assertFalse((UPLOADS / "checked_bot").exists())

    def test_file_names_cannot_escape_the_upload_folder(self):
        r = self.start("safe_names", files=(("../../bots/evil.md", MD), ("..\\..\\x.md", MD), ("a.md", MD),
                                            ("A.md", MD)))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["files"], ["evil.md", "x.md", "a.md", "A_2.md"])
        self.assertEqual(sorted(p.name for p in (UPLOADS / "safe_names").iterdir()),
                         ["A_2.md", "a.md", "evil.md", "x.md"])
        self.assertFalse((ROOT / "bots" / "evil.md").exists())
        self.wait(r.json()["id"])
        self.root.post(f"/admin/builds/{r.json()['id']}/decision", json={"decision": "reject"})

    def test_build_review_approve_serves_the_bot_at_once(self):
        r = self.start("handbook_bot", files=(("handbook.md", MD), ("policy.pdf", PDF)))
        self.assertEqual(r.status_code, 201)
        build_id = r.json()["id"]
        self.assertEqual(self.wait(build_id)["status"], "review")

        stages = [e["stage"] for e in self.events(build_id)]
        self.assertEqual(stages, ["started", "ingest", "draft", "validate", "write_tests", "review"])
        detail = self.root.get(f"/admin/builds/{build_id}").json()
        self.assertEqual(detail["proposal"]["config"]["name"], "Handbook Helper")
        self.assertNotIn("handbook_bot", [b["id"] for b in self.root.get("/bots").json()])  # not before approval

        r = self.root.post(f"/admin/builds/{build_id}/decision", json={"decision": "approve"})
        self.assertEqual(r.json()["status"], "saved")
        self.assertTrue((WORK / "bots" / "handbook_bot.yaml").exists())
        self.assertIn("handbook_bot", [b["id"] for b in self.root.get("/bots").json()])  # no restart needed
        self.assertNotIn("handbook_bot", [b["id"] for b in self.alice.get("/bots").json()])  # members need a grant
        again = self.root.post(f"/admin/builds/{build_id}/decision", json={"decision": "approve"})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.start("handbook_bot").status_code, 400)  # the id is taken now

        actions = [e["action"] for e in self.root.get("/admin/audit").json()["entries"]]
        self.assertIn("build.start", actions)
        self.assertIn("build.decision", actions)

    def test_reject_deletes_the_uploads_and_saves_nothing(self):
        build_id = self.start("rejected_bot").json()["id"]
        self.wait(build_id)
        r = self.root.post(f"/admin/builds/{build_id}/decision", json={"decision": "reject"})
        self.assertEqual(r.json()["status"], "rejected")
        self.assertFalse((UPLOADS / "rejected_bot").exists())
        self.assertFalse((WORK / "bots" / "rejected_bot.yaml").exists())

    def test_a_failed_build_reports_its_problems_and_cleans_up(self):
        build_id = self.start("failing_bot", use_case="FAIL: documents that cannot support a bot").json()["id"]
        build = self.wait(build_id)
        self.assertEqual(build["status"], "failed")
        self.assertIn("not answered", build["problems"][0])
        self.assertFalse((UPLOADS / "failing_bot").exists())

    def test_events_resume_after_the_last_one_seen(self):
        build_id = self.start("resume_bot").json()["id"]
        self.wait(build_id)
        self.assertEqual([e["n"] for e in self.events(build_id, {"Last-Event-ID": "4"})], [5, 6])
        self.root.post(f"/admin/builds/{build_id}/decision", json={"decision": "reject"})

    def test_builds_queue_and_run_one_at_a_time(self):
        GATE.clear()  # the first build waits in "ingest"
        try:
            first = self.start("first_bot").json()["id"]
            deadline = time.monotonic() + 5
            while self.root.get(f"/admin/builds/{first}").json()["status"] != "running":
                self.assertLess(time.monotonic(), deadline, "the worker never took the first build")
                time.sleep(0.05)
            r = self.start("second_bot")  # accepted, and waits in the queue
            self.assertEqual(r.status_code, 201)
            second = r.json()["id"]
            time.sleep(0.3)
            self.assertEqual(self.root.get(f"/admin/builds/{second}").json()["status"], "queued")
            self.assertEqual(self.start("second_bot").status_code, 400)  # one build per bot id at a time
        finally:
            GATE.set()
        for build_id in (first, second):
            self.assertEqual(self.wait(build_id)["status"], "review")
            self.root.post(f"/admin/builds/{build_id}/decision", json={"decision": "reject"})

    def test_two_admins_deciding_at_once_one_wins(self):
        build_id = self.start("contested_bot").json()["id"]
        self.wait(build_id)
        from engine.builds import UploadProblem
        manager = api.app.state.builds

        async def both():
            return await asyncio.gather(manager.decide(build_id, "reject"), manager.decide(build_id, "reject"),
                                        return_exceptions=True)
        results = self.client.portal.call(both)
        self.assertEqual(sum(isinstance(r, UploadProblem) for r in results), 1)
        self.assertEqual(self.root.get(f"/admin/builds/{build_id}").json()["status"], "rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
