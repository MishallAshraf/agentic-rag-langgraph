"""Step 15 - The bot builder as a service: uploads in, a reviewed bot out, without a restart.

Step 10 built bots from the command line. Here the same builder graph
(step10_builder.build_builder) runs as a background job inside the API:

    POST /admin/builds                 documents + bot id + use case -> a build, started at once
    GET  /admin/builds/{id}/events     its progress (Server-Sent Events; replays from Last-Event-ID)
    GET  /admin/builds/{id}            its state, and the proposal once it waits for review
    POST /admin/builds/{id}/decision   approve (saves and serves the bot) or reject (deletes the uploads)

Why a job and not one request: indexing and drafting take one to several
minutes. A request that long breaks on a reload or a proxy timeout, and the
work would be lost with it. The job keeps running; any page can reconnect to
its events, which are numbered so nothing is missed or repeated.

Lampus Phase 1, Step 8 (course topic 5): the work runs in a separate worker
process (engine/worker.py), not in the API. The API stores the build in Postgres
and puts a job on a Redis queue; a worker takes the job, runs the builder graph
and writes each progress event to Postgres, then announces it on Redis pub/sub so
whichever API copy a page is connected to can pass it on. A build survives the
API copy that started it, and a worker that dies mid-build has its job taken
over by another. Each worker runs one build at a time: indexing is CPU- and
memory-heavy.
"""

import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from langgraph.types import Command
from psycopg.types.json import Jsonb

from engine import shared, webhooks
from engine.bot_config import ID_PATTERN, ROOT

log = logging.getLogger("builds")

UPLOADS_DIR = ROOT / "documents" / "uploads"   # gitignored with the rest of documents/
ALLOWED_TYPES = {".pdf": b"%PDF", ".docx": b"PK\x03\x04", ".md": None}  # extension -> first bytes
MAX_FILES = 20
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
USE_CASE_CHARS = (20, 1000)
KEEP_FINISHED = 20  # builds listed on the admin page
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")

STAGES = {  # builder node -> what the user is told
    "ingest": "Indexed the documents",
    "survey": "Surveyed the documents",
    "draft": "Drafted the bot",
    "validate": "Checked the draft",
    "write_tests": "Wrote test questions",
    "save": "Saved the bot",
}


class UploadProblem(ValueError):
    """A file or field the server will not accept; the message is safe to show."""


def safe_filename(name: str, taken: set[str]) -> str:
    # Only the last path part, only harmless characters: a name like "../../bots/x.yaml" becomes "bots_x.yaml"
    # at most, and always lands inside the upload folder.
    base = SAFE_NAME.sub("_", Path(name.replace("\\", "/")).name).strip(" .") or "file"
    stem, suffix = Path(base).stem[:80] or "file", Path(base).suffix.lower()
    candidate, n = f"{stem}{suffix}", 2
    while candidate.lower() in taken:
        candidate, n = f"{stem}_{n}{suffix}", n + 1
    taken.add(candidate.lower())
    return candidate


def check_upload(name: str, data: bytes) -> None:
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_TYPES:
        raise UploadProblem(f"{name}: only .pdf, .docx and .md files are accepted")
    if not data:
        raise UploadProblem(f"{name}: the file is empty")
    magic = ALLOWED_TYPES[suffix]
    if magic and not data.startswith(magic):  # the extension must match the content
        raise UploadProblem(f"{name}: this is not a real {suffix} file")
    if suffix == ".md":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadProblem(f"{name}: Markdown files must be UTF-8 text") from None


ACTIVE = ("queued", "running", "review", "deciding")
WORKING = ("queued", "running")  # the worker still has something to do
MAX_ATTEMPTS = 3                 # a build whose worker keeps dying is given up on
QUEUE = "builds"


def _summary(row: dict) -> dict:
    return {"id": row["id"], "bot_id": row["bot_id"], "status": row["status"], "by": row["by_user"],
            "started": row["started"].timestamp(), "files": row["files"], "use_case": row["use_case"],
            "worker": row["worker"], "attempts": row["attempts"]}


class BuildStore:
    """Builds and their progress events in Postgres, readable by every API copy and the worker."""

    JSON_FIELDS = {"files", "proposal", "problems", "saved"}

    def __init__(self, pool):
        self.pool = pool

    async def create(self, bot_id: str, use_case: str, folder: str, files: list[str], by: str) -> dict:
        async with self.pool.connection() as conn:
            return await (await conn.execute(
                "INSERT INTO builds (id, bot_id, use_case, folder, files, by_user, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'queued') RETURNING *",
                (uuid.uuid4().hex, bot_id, use_case, folder, Jsonb(files), by))).fetchone()

    async def get(self, build_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT * FROM builds WHERE id = %s", (build_id,))).fetchone()

    async def recent(self, limit: int = KEEP_FINISHED) -> list[dict]:
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT * FROM builds ORDER BY started DESC LIMIT %s",
                                             (limit,))).fetchall()

    async def update(self, build_id: str, only_from: tuple[str, ...] | None = None, **fields) -> dict | None:
        """Change fields; with only_from, only if the status is one of those (None if it wasn't).

        The status check and the change are one statement, so two API copies
        deciding the same build can't both win.
        """
        sets = ", ".join(f"{k} = %({k})s" for k in fields)
        params = {k: Jsonb(v) if k in self.JSON_FIELDS else v for k, v in fields.items()}
        where = "id = %(id)s" + (" AND status = ANY(%(from)s)" if only_from else "")
        async with self.pool.connection() as conn:
            return await (await conn.execute(f"UPDATE builds SET {sets}, updated = now() WHERE {where} RETURNING *",
                                             {**params, "id": build_id, "from": list(only_from or [])})).fetchone()

    async def add_event(self, build_id: str, stage: str, status: str, **data) -> dict:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # One writer per build at a time (the worker while it runs, then the deciding copy);
                # the row lock makes numbering safe even if that ever changes.
                await conn.execute("SELECT 1 FROM builds WHERE id = %s FOR UPDATE", (build_id,))
                n = (await (await conn.execute(
                    "SELECT coalesce(max(n), 0) + 1 AS n FROM build_events WHERE build_id = %s",
                    (build_id,))).fetchone())["n"]
                event = {"n": n, "stage": stage, "status": status, **data}
                await conn.execute("INSERT INTO build_events (build_id, n, event) VALUES (%s, %s, %s)",
                                   (build_id, n, Jsonb(event)))
        await shared.publish(f"build:{build_id}", {"n": n})  # wakes anyone following this build
        return event

    async def events_after(self, build_id: str, after: int) -> list[dict]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT event FROM build_events WHERE build_id = %s AND n > %s ORDER BY n",
                (build_id, after))).fetchall()
        return [r["event"] for r in rows]

    async def active_bot_ids(self) -> set[str]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute("SELECT DISTINCT bot_id FROM builds WHERE status = ANY(%s)",
                                             (list(ACTIVE),))).fetchall()
        return {r["bot_id"] for r in rows}


async def detail(store: BuildStore, row: dict) -> dict:
    return {**_summary(row), "events": await store.events_after(row["id"], 0), "proposal": row["proposal"],
            "problems": row["problems"], "saved": row["saved"]}


class BuildManager:
    """The API's side of builds: accept uploads, queue the job, report progress, take the review decision."""

    def __init__(self, store: BuildStore, make_graph: Callable, bot_exists: Callable[[str], bool],
                 on_saved: Callable[[str], None]):
        self.store = store
        self.make_graph = make_graph   # () -> the builder graph, on the shared Postgres checkpointer
        self.bot_exists = bot_exists
        self.on_saved = on_saved       # bot_id -> serve the new bot in this copy

    # ---------------------------------------------------------------- starting

    async def check_request(self, bot_id: str, use_case: str) -> None:
        if not ID_PATTERN.match(bot_id):
            raise UploadProblem("Bot id: 3-40 characters, lowercase letters, digits and underscores, "
                                "starting with a letter.")
        if self.bot_exists(bot_id):
            raise UploadProblem(f"A bot called '{bot_id}' already exists. Choose another id.")
        if bot_id in await self.store.active_bot_ids():
            raise UploadProblem(f"A build of '{bot_id}' is queued, running or waiting for review. "
                                "Wait for it, or reject it first.")
        if not USE_CASE_CHARS[0] <= len(use_case.strip()) <= USE_CASE_CHARS[1]:
            raise UploadProblem(f"Describe the use case in {USE_CASE_CHARS[0]} to {USE_CASE_CHARS[1]} characters.")

    def store_files(self, bot_id: str, files: list[tuple[str, bytes]]) -> tuple[str, list[str]]:
        """Checks every file first, then writes them into a fresh folder for this bot."""
        if not 1 <= len(files) <= MAX_FILES:
            raise UploadProblem(f"Upload 1 to {MAX_FILES} files.")
        if sum(len(d) for _, d in files) > MAX_TOTAL_BYTES:
            raise UploadProblem(f"The files add up to more than {MAX_TOTAL_BYTES // 2**20} MB.")
        for name, data in files:
            if len(data) > MAX_FILE_BYTES:
                raise UploadProblem(f"{name}: larger than {MAX_FILE_BYTES // 2**20} MB")
            check_upload(name, data)
        folder = UPLOADS_DIR / bot_id
        shutil.rmtree(folder, ignore_errors=True)  # left over from a rejected or failed build
        folder.mkdir(parents=True)
        taken: set[str] = set()
        names = []
        for name, data in files:
            safe = safe_filename(name, taken)
            (folder / safe).write_bytes(data)
            names.append(safe)
        return folder.relative_to(ROOT).as_posix(), names

    async def start(self, bot_id: str, use_case: str, folder: str, files: list[str], by: str) -> dict:
        row = await self.store.create(bot_id, use_case.strip(), folder, files, by)
        await shared.enqueue(QUEUE, {"build": row["id"]})  # a worker picks it up
        return _summary(row)

    # ---------------------------------------------------------------- following

    async def follow(self, build_id: str, after: int = 0):
        """Every event after number `after`, then new ones as they happen, until the worker is done.

        Subscribe first, then read: an event written between the two is still read.
        Pub/sub only says "something new"; the events themselves come from Postgres,
        so a page can reconnect to any copy and miss nothing.
        """
        sent, listener = after, await shared.subscribe(f"build:{build_id}")
        try:
            while True:
                for e in await self.store.events_after(build_id, sent):
                    yield e
                    sent = e["n"]
                row = await self.store.get(build_id)
                if row["status"] not in WORKING and not await self.store.events_after(build_id, sent):
                    return
                # Wait for a "something new" message; re-check every 15 s in case one was lost.
                await listener.get_message(timeout=15)
        finally:
            await listener.aclose()

    # ---------------------------------------------------------------- deciding

    async def decide(self, build_id: str, decision: str) -> dict:
        row = await self.store.get(build_id)
        if row is None or row["status"] != "review":
            raise UploadProblem("This build is not waiting for review.")
        if decision == "approve" and self.bot_exists(row["bot_id"]):  # made by someone else meanwhile
            raise UploadProblem(f"A bot called '{row['bot_id']}' exists now. Reject this build.")
        if await self.store.update(build_id, only_from=("review",), status="deciding") is None:
            raise UploadProblem("This build is not waiting for review.")  # another admin got there first
        config = {"configurable": {"thread_id": row["thread"]}}
        try:
            # The builder's state is in the shared checkpointer, so this copy can resume
            # the graph the worker paused, wherever that worker ran.
            result = await self.make_graph().ainvoke(Command(resume=decision), config)
        except Exception:
            await self.store.update(build_id, status="review")  # nothing was saved: try again
            raise
        if decision == "approve" and result.get("saved"):
            await self.store.update(build_id, status="saved", saved=result["saved"])
            self.on_saved(row["bot_id"])
            await shared.publish("bots", {"saved": row["bot_id"]})  # every other copy serves it too
            await self.store.add_event(build_id, "saved", "saved", message="Saved: " + ", ".join(result["saved"]))
        else:
            await self.store.update(build_id, status="rejected", proposal=None)
            shutil.rmtree(ROOT / row["folder"], ignore_errors=True)
            await self.store.add_event(build_id, "rejected", "rejected",
                                       message="Rejected; the uploaded files were deleted")
        decided = await self.store.get(build_id)
        await webhooks.emit("build.decided", {"build_id": build_id, "bot_id": row["bot_id"],
                                              "status": decided["status"]})
        return decided

    # ---------------------------------------------------------------- housekeeping

    async def clean_orphans(self) -> list[str]:
        """At start-up: uploads of builds that will never become bots."""
        keep, removed = await self.store.active_bot_ids(), []
        if UPLOADS_DIR.exists():
            for folder in UPLOADS_DIR.iterdir():
                if folder.is_dir() and not self.bot_exists(folder.name) and folder.name not in keep:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed.append(folder.name)
        return removed


# ------------------------------------------------------------------ the worker's side

def _describe(node: str, data: dict) -> str:
    if node == "draft" and data.get("spec"):
        return f"Drafted \"{data['spec'].get('name', '')}\""
    if node == "validate":
        problems = data.get("problems") or []
        return f"Found {len(problems)} problem(s); drafting again" if problems else "The draft passed every check"
    logs = data.get("log") or []
    text = logs[-1] if logs else STAGES[node]
    return text[:1].upper() + text[1:]


async def _fail(store: BuildStore, row: dict, problems: list[str]) -> None:
    await store.update(row["id"], status="failed", problems=problems)
    shutil.rmtree(ROOT / row["folder"], ignore_errors=True)
    await store.add_event(row["id"], "failed", "failed", problems=problems)
    await webhooks.emit("build.finished", {"build_id": row["id"], "bot_id": row["bot_id"], "status": "failed"})


async def run_build(store: BuildStore, make_graph: Callable, build_id: str, worker: str) -> None:
    """Run one build up to its review (in the worker). Safe to call twice for the same build."""
    row = await store.get(build_id)
    if row is None or row["status"] not in WORKING:
        return  # already finished: the job was delivered again after a worker crashed late
    attempt = row["attempts"] + 1
    if attempt > MAX_ATTEMPTS:
        await _fail(store, row, [f"The build was given up after {MAX_ATTEMPTS} attempts (the worker kept stopping)."])
        return
    thread = f"build:{build_id}:{attempt}"  # a fresh checkpoint thread per attempt
    row = await store.update(build_id, status="running", attempts=attempt, worker=worker, thread=thread)
    config = {"configurable": {"thread_id": thread}}
    try:
        extra = {"attempt": attempt} if attempt > 1 else {}
        await store.add_event(build_id, "started", "running", files=len(row["files"]), **extra)
        graph = make_graph()
        async for update in graph.astream({"bot_id": row["bot_id"], "use_case": row["use_case"],
                                           "folder": row["folder"], "log": []}, config, stream_mode="updates"):
            for node, data in update.items():
                if node in STAGES:
                    await store.add_event(build_id, node, "running", message=_describe(node, data or {}))
        state = await graph.aget_state(config)
        if state.interrupts:
            await store.update(build_id, status="review", proposal=state.interrupts[0].value)
            await store.add_event(build_id, "review", "review", message="Ready for review")
            await webhooks.emit("build.finished", {"build_id": build_id, "bot_id": row["bot_id"], "status": "review"})
        else:
            await _fail(store, row, state.values.get("problems") or ["The builder stopped without a proposal."])
    except (Exception, SystemExit) as e:  # ingest raises SystemExit when no file can be read
        log.exception("build %s failed", build_id)
        await _fail(store, row, [f"The build failed: {type(e).__name__}: {e}"])
