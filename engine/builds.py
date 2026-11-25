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

Builds live in memory. A restart loses builds that were not yet approved
(their uploads are deleted at the next start); saved bots are files, so they
survive. One build runs at a time: indexing is CPU- and memory-heavy, and this
machine has 8 GB.
"""

import asyncio
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from langgraph.types import Command

from engine.bot_config import ID_PATTERN, ROOT

log = logging.getLogger("builds")

UPLOADS_DIR = ROOT / "documents" / "uploads"   # gitignored with the rest of documents/
ALLOWED_TYPES = {".pdf": b"%PDF", ".docx": b"PK\x03\x04", ".md": None}  # extension -> first bytes
MAX_FILES = 20
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
USE_CASE_CHARS = (20, 1000)
KEEP_FINISHED = 20  # finished builds kept for viewing
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


@dataclass
class Build:
    id: str
    bot_id: str
    use_case: str
    folder: str                      # relative to the project, as Knowledge expects
    files: list[str]
    by: str
    status: str = "running"          # running -> review -> saved | rejected; or failed
    events: list[dict] = field(default_factory=list)
    proposal: dict | None = None     # config and tests, while waiting for review
    problems: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None
    graph: object = None             # the builder graph, kept for the review decision

    @property
    def finished(self) -> bool:
        return self.status in ("saved", "rejected", "failed")

    def summary(self) -> dict:
        return {"id": self.id, "bot_id": self.bot_id, "status": self.status, "by": self.by,
                "started": self.started, "files": self.files, "use_case": self.use_case}


class BuildManager:
    """Runs builder graphs as background jobs and keeps their events for anyone who reconnects."""

    def __init__(self, make_graph: Callable, bot_exists: Callable[[str], bool], on_saved: Callable[[str], None]):
        self.make_graph = make_graph   # () -> a compiled builder graph with a checkpointer
        self.bot_exists = bot_exists
        self.on_saved = on_saved       # bot_id -> serve the new bot
        self.builds: dict[str, Build] = {}
        self.lock = asyncio.Lock()     # one build runs at a time

    # ---------------------------------------------------------------- starting

    def busy(self) -> Build | None:
        return next((b for b in self.builds.values() if b.status == "running"), None)

    def check_request(self, bot_id: str, use_case: str) -> None:
        if not ID_PATTERN.match(bot_id):
            raise UploadProblem("Bot id: 3-40 characters, lowercase letters, digits and underscores, "
                                "starting with a letter.")
        if self.bot_exists(bot_id):
            raise UploadProblem(f"A bot called '{bot_id}' already exists. Choose another id.")
        if any(b.bot_id == bot_id and b.status == "review" for b in self.builds.values()):
            raise UploadProblem(f"A build of '{bot_id}' is waiting for review. Approve or reject it first.")
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

    async def start(self, bot_id: str, use_case: str, folder: str, files: list[str], by: str) -> Build:
        build = Build(id=uuid.uuid4().hex, bot_id=bot_id, use_case=use_case.strip(), folder=folder,
                      files=files, by=by)
        self.builds[build.id] = build
        self._forget_old()
        build.task = asyncio.create_task(self._run(build))
        return build

    # ---------------------------------------------------------------- running

    async def _emit(self, build: Build, stage: str, **data) -> None:
        async with build.changed:
            build.events.append({"n": len(build.events) + 1, "stage": stage, "status": build.status, **data})
            build.changed.notify_all()

    async def _run(self, build: Build) -> None:
        config = {"configurable": {"thread_id": build.id}}
        try:
            async with self.lock:
                await self._emit(build, "started", files=len(build.files))
                build.graph = graph = self.make_graph()
                async for update in graph.astream({"bot_id": build.bot_id, "use_case": build.use_case,
                                                   "folder": build.folder, "log": []}, config,
                                                  stream_mode="updates"):
                    for node, data in update.items():
                        if node in STAGES:
                            await self._emit(build, node, message=self._describe(node, data or {}))
                state = await graph.aget_state(config)
                if state.interrupts:
                    build.proposal = state.interrupts[0].value
                    build.status = "review"
                    await self._emit(build, "review", message="Ready for review")
                else:
                    build.problems = state.values.get("problems") or ["The builder stopped without a proposal."]
                    await self._fail(build)
        except (Exception, SystemExit) as e:  # ingest raises SystemExit when no file can be read
            log.exception("build %s failed", build.id)
            build.problems = [f"The build failed: {type(e).__name__}: {e}"]
            await self._fail(build)

    def _describe(self, node: str, data: dict) -> str:
        if node == "draft" and data.get("spec"):
            return f"Drafted \"{data['spec'].get('name', '')}\""
        if node == "validate":
            problems = data.get("problems") or []
            return f"Found {len(problems)} problem(s); drafting again" if problems else "The draft passed every check"
        logs = data.get("log") or []
        text = logs[-1] if logs else STAGES[node]
        return text[:1].upper() + text[1:]

    async def _fail(self, build: Build) -> None:
        build.status = "failed"
        shutil.rmtree(ROOT / build.folder, ignore_errors=True)
        await self._emit(build, "failed", problems=build.problems)

    # ---------------------------------------------------------------- deciding

    async def decide(self, build: Build, decision: str) -> Build:
        if build.status != "review":
            raise UploadProblem("This build is not waiting for review.")
        if decision == "approve" and self.bot_exists(build.bot_id):  # made by someone else meanwhile
            raise UploadProblem(f"A bot called '{build.bot_id}' exists now. Reject this build.")
        build.status = "deciding"
        config = {"configurable": {"thread_id": build.id}}
        try:
            result = await build.graph.ainvoke(Command(resume=decision), config)
        except Exception:
            build.status = "review"  # nothing was saved: the reviewer may try again
            raise
        if decision == "approve" and result.get("saved"):
            build.saved, build.status = result["saved"], "saved"
            self.on_saved(build.bot_id)
            await self._emit(build, "saved", message="Saved: " + ", ".join(build.saved))
        else:
            build.status = "rejected"
            shutil.rmtree(ROOT / build.folder, ignore_errors=True)
            await self._emit(build, "rejected", message="Rejected; the uploaded files were deleted")
        build.proposal = build.proposal if build.status == "saved" else None
        return build

    # ---------------------------------------------------------------- following

    async def follow(self, build: Build, after: int = 0):
        """Every event after number `after`, then new ones as they happen, until the build stops running."""
        sent = after
        while True:
            async with build.changed:
                await build.changed.wait_for(lambda: len(build.events) > sent or build.status != "running")
                new = build.events[sent:]
            for e in new:
                yield e
            sent += len(new)
            if build.status != "running" and sent >= len(build.events):
                return

    # ---------------------------------------------------------------- housekeeping

    def _forget_old(self) -> None:
        finished = sorted((b for b in self.builds.values() if b.finished), key=lambda b: b.started)
        for b in finished[:-KEEP_FINISHED]:
            del self.builds[b.id]

    @staticmethod
    def clean_orphans(bot_exists: Callable[[str], bool]) -> list[str]:
        """At start-up: uploads of builds that never became bots (the server stopped first)."""
        removed = []
        if UPLOADS_DIR.exists():
            for folder in UPLOADS_DIR.iterdir():
                if folder.is_dir() and not bot_exists(folder.name):
                    shutil.rmtree(folder, ignore_errors=True)
                    removed.append(folder.name)
        return removed

    async def close(self) -> None:
        for b in self.builds.values():
            if b.task and not b.task.done():
                b.task.cancel()
