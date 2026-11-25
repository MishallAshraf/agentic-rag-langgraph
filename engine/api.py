"""Step 11 - The agent behind an HTTP API, streaming over Server-Sent Events.
Step 13 - With users: sign-in, per-bot access, private conversations, quotas, audit log.
Step 15 - With the bot builder: admins upload documents and approve new bots (builds.py).

    POST /auth/login                               sign in -> session cookie
    POST /auth/logout                              sign out (the session is deleted on the server)
    GET  /auth/me                                  who am I, and today's usage
    GET  /health                                   is the server up (no sign-in needed)
    GET  /bots                                     the bots this user may use, public fields only
    GET  /bots/{bot}                               one bot (never its system prompt)
    GET  /bots/{bot}/threads                       this user's conversations with the bot
    POST /bots/{bot}/threads                       start a conversation -> thread_id
    GET  /bots/{bot}/threads/{thread}              its messages, and a pending approval if any
    POST /bots/{bot}/threads/{thread}/messages     send a message; the reply streams back (SSE)
    POST /bots/{bot}/threads/{thread}/approval     approve or reject a paused tool call (SSE)
    GET  /admin/audit                              latest audit events and the chain check (admins)
    POST /admin/builds                             upload documents and start building a bot (admins)
    GET  /admin/builds                             recent builds
    GET  /admin/builds/{build}                     one build: progress, and its proposal when ready
    GET  /admin/builds/{build}/events              its progress as it happens (SSE, resumable)
    POST /admin/builds/{build}/decision            approve (the bot is served at once) or reject

Why Server-Sent Events: an answer takes 5-30 s (guard, search, grading,
fact check). SSE keeps one HTTP response open and sends events as they
happen, so the user sees progress and the answer as it is written. It is
plain HTTP, one direction (server to browser), and reconnects by itself;
WebSockets would add a two-way channel this API does not need.

Events in a stream, in order:
    status    {"stage": ...}                 received, guard verdict, tool calls, fact check
    token     {"message_id", "text"}         part of the answer as the model writes it
    approval  {"tool_calls": [...]}          the turn paused; POST .../approval to continue
    error     {"type"}                       the turn failed; the conversation was repaired
    done      {"answer", "sources", ...}     the final answer: this, not the tokens, is authoritative

Tokens are a preview. The fact check (Step 7) can reject a draft after it was
streamed; the next draft then streams under a new message_id, and "done"
carries the answer that was actually kept.

Authorization answers "not found" both for things that do not exist and for
things that belong to someone else, so nobody can probe which bots or
conversations exist. See auth.py for the rules and the audit log.

    python -m engine.api          # http://127.0.0.1:8000, docs at /docs
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.datastructures import UploadFile
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from engine import analysis
from engine import step9_agent
from engine import step14_research
from engine.auth import SESSION_TTL_S, AuthStore, User
from engine.db import postgres_checkpointer
from engine.builds import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, Build, BuildManager, UploadProblem
from engine.bot_config import BotConfig, Theme, load_bot
from engine.step7_agent import SOURCE_TOOLS, current_turn
from engine.tool_registry import load_tools, uses_user_data

load_dotenv()  # before the API_* settings below are read
log = logging.getLogger("api")

BOTS_DIR = Path(__file__).resolve().parent.parent / "bots"
TURN_TIMEOUT_S = float(os.getenv("API_TURN_TIMEOUT", "120"))  # latency budget for one turn
RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "10"))            # turns per user per window
RATE_WINDOW_S = 60
LOGIN_LIMIT = 5                                                # sign-in attempts per address and name per minute
MAX_MESSAGE_CHARS = 4000
CORS_ORIGINS = os.getenv("API_CORS_ORIGINS", "http://localhost:5173").split(",")  # Vite dev server
COOKIE_NAME = "session"
COOKIE_SECURE = os.getenv("API_COOKIE_SECURE", "false").lower() == "true"  # true behind HTTPS
THREAD_PATTERN = re.compile(r"^[a-f0-9]{32}$")
SOURCE_PATTERN = re.compile(r"^\[\d+\] (.+)$", re.MULTILINE)  # "[1] file, page 4" in search results
ANSWER_NODES = {"agent", "write"}  # nodes whose model output is the answer (Step 7 agent, Step 14 writer)


# ------------------------------------------------------------------ bots and their graphs

@dataclass
class BotEntry:
    config: BotConfig
    graphs: dict = field(default_factory=dict)  # "" for a shared graph, or one per user
    build_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Platform:
    """Everything the server shares between requests."""

    def __init__(self, checkpointer, llm):
        self.checkpointer, self.llm = checkpointer, llm
        self.bots: dict[str, BotEntry] = {}
        self.thread_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def load_bots(self):
        for path in sorted(BOTS_DIR.glob("*.yaml")):
            try:
                bot = load_bot(path)
            except Exception as e:  # one broken file must not take the others down
                log.error("skipping %s: %s", path.name, e)
                continue
            self.bots[bot.id] = BotEntry(bot)

    def add_bot(self, bot_id: str):
        """Serve a bot the builder just saved, without a restart (Step 15)."""
        bot = load_bot(BOTS_DIR / f"{bot_id}.yaml")
        self.bots[bot.id] = BotEntry(bot)

    async def graph_for(self, entry: BotEntry, user: User):
        # A bot whose tools keep per-user data (notes) gets one graph per user,
        # with tools bound to that user's folder; other bots share one graph.
        key = user.username if uses_user_data(entry.config) else ""
        async with entry.build_lock:
            if key not in entry.graphs:
                if entry.config.research:  # Step 14: a research team over other bots' documents
                    bots = {bot_id: e.config for bot_id, e in self.bots.items()}
                    entry.graphs[key] = step14_research.build_graph(entry.config, bots, self.llm, self.checkpointer)
                else:
                    tools = await load_tools(entry.config.tools, entry.config, user=key or None)
                    entry.graphs[key] = step9_agent.build_graph(entry.config, tools, self.llm, self.checkpointer)
        return entry.graphs[key]


class RateLimiter:
    """Sliding window per key: at most `limit` requests in `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def retry_after(self, key: str, record: bool = True) -> int:
        """0 if the request may go ahead (and, if record, count it), else seconds to wait."""
        now, hits = time.monotonic(), self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return int(self.window - (now - hits[0])) + 1
        if record:
            hits.append(now)
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    from engine.llm import get_llm

    app.state.auth = await AuthStore().open()
    try:
        async with postgres_checkpointer() as checkpointer:  # conversations (Lampus Phase 1, Step 4)
            app.state.platform = Platform(checkpointer, get_llm())
            app.state.platform.load_bots()
            app.state.limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
            app.state.login_limiter = RateLimiter(LOGIN_LIMIT, 60)
            app.state.builds = make_build_manager(app.state.platform)
            try:
                yield
            finally:
                await app.state.builds.close()
    finally:
        await app.state.auth.close()


def make_build_manager(platform: Platform) -> BuildManager:
    from engine.llm import get_llm
    from engine.step10_builder import BUILDER_MODEL, build_builder

    def bot_exists(bot_id: str) -> bool:
        return bot_id in platform.bots

    removed = BuildManager.clean_orphans(bot_exists)
    if removed:
        log.info("deleted uploads of unfinished builds: %s", ", ".join(removed))
    # A fresh graph per build; its in-memory checkpoint only has to last until the review.
    return BuildManager(lambda: build_builder(get_llm(model=BUILDER_MODEL), InMemorySaver()),
                        bot_exists, platform.add_bot)


app = FastAPI(title="Agentic RAG bots", version="0.15.0", lifespan=lifespan)
# No allow_credentials: a cross-origin page can never send the session cookie.
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"])


# ------------------------------------------------------------------ request and response shapes

class BotPublic(BaseModel):
    """What a client may know about a bot. The system prompt and tool setup stay on the server."""
    id: str
    name: str
    description: str
    welcome: str
    suggested_questions: list[str]
    topics: list[str]
    theme: Theme


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ApprovalIn(BaseModel):
    decision: Literal["approve", "reject"]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    sources: list[str] = []  # for an answer: what its turn retrieved


class ThreadOut(BaseModel):
    thread_id: str
    messages: list[ChatMessage]
    pending_approval: dict | None = None


# ------------------------------------------------------------------ who is asking, and may they

def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def current_user(request: Request) -> User:
    user = await request.app.state.auth.user_for_session(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only admins can do this.")
    return user


async def bot_for(request: Request, user: User, bot_id: str) -> BotEntry:
    """The bot, if this user may use it. Otherwise 404, the same as for a bot that does not exist."""
    auth = request.app.state.auth
    entry = request.app.state.platform.bots.get(bot_id)
    if entry is not None and await auth.can_use(user, bot_id):
        return entry
    if entry is not None:
        await auth.audit(user.username, "access.denied", bot_id=bot_id,
                         detail={"reason": "bot not granted"}, ip=client_ip(request))
    raise HTTPException(404, f"No bot '{bot_id}'")


async def own_thread(request: Request, user: User, bot_id: str, thread_id: str):
    """Only the user who started a conversation may read or continue it."""
    if not THREAD_PATTERN.fullmatch(thread_id):
        raise HTTPException(404, "No such conversation")
    owns = await request.app.state.auth.owns_thread(user, bot_id, thread_id)
    if owns:
        return
    if owns is False:  # it exists, but is someone else's (or another bot's)
        await request.app.state.auth.audit(user.username, "access.denied", bot_id=bot_id, thread_id=thread_id,
                                           detail={"reason": "not the owner"}, ip=client_ip(request))
    raise HTTPException(404, "No such conversation")


async def config_for(request: Request, user: User, entry: BotEntry, thread_id: str) -> dict:
    """The graph config for this user's turn. A research bot gets the sources this user may read,
    so its agents act with the user's permissions, never more (Step 14)."""
    config = step9_agent.turn_config(entry.config, thread_id)
    if entry.config.research:
        auth = request.app.state.auth
        allowed = [s for s in entry.config.research.sources if await auth.can_use(user, s)]
        config["configurable"] = {**config["configurable"], "allowed_sources": allowed}
    return config


def rate_limit(request: Request, user: User):
    wait = request.app.state.limiter.retry_after(user.username)
    if wait:
        raise HTTPException(429, "Too many messages; slow down.", headers={"Retry-After": str(wait)})


async def check_quota(request: Request, user: User, bot_id: str):
    problem = await request.app.state.auth.over_quota(user.username)
    if problem:
        await request.app.state.auth.audit(user.username, "quota.exceeded", bot_id=bot_id, ip=client_ip(request))
        raise HTTPException(429, problem)


# ------------------------------------------------------------------ streaming a turn

async def pending_approval(graph, config) -> dict | None:
    snapshot = await graph.aget_state(config, subgraphs=True)
    interrupts = list(snapshot.interrupts) + [i for t in snapshot.tasks for i in t.interrupts]
    return interrupts[0].value if interrupts else None


def sources_in(messages) -> list[str]:
    found = {s.strip() for m in messages if isinstance(m, ToolMessage) and m.name in SOURCE_TOOLS
             for s in SOURCE_PATTERN.findall(m.text)}
    # A research answer carries its sources itself (Step 14: the writer never calls tools).
    for m in messages:
        if isinstance(m, AIMessage):
            found.update(m.response_metadata.get("sources", []))
    return sorted(found)


def event(name: str, data: dict) -> ServerSentEvent:
    return ServerSentEvent(event=name, data=json.dumps(data, ensure_ascii=False))


def describe(node: str, update) -> list[ServerSentEvent]:
    """Turn a graph step into progress events. Internal details (prompts, raw tool output) stay out."""
    update = update or {}
    if node == "guard_input" and update.get("guard"):
        return [event("status", {"stage": "guard", "result": update["guard"].split(":")[0]})]
    if node == "start_turn" and update.get("messages"):
        return [event("status", {"stage": "compact", "removed": len(update["messages"])})]
    if node == "agent":
        last = (update.get("messages") or [None])[-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return [event("status", {"stage": "tool_call", "name": c["name"], "args": c["args"]})
                    for c in last.tool_calls]
    if node == "tools":
        return [event("status", {"stage": "tool_result", "name": m.name})
                for m in update.get("messages", []) if isinstance(m, ToolMessage)]
    if node == "check_answer":
        return [event("status", {"stage": "fact_check",
                                 "result": "revising" if update.get("feedback") else "supported"})]
    if node == "guard_output" and update.get("guard"):
        return [event("status", {"stage": "output_guard", "result": "blocked"})]
    # Step 14: the research team
    if node == "plan" and update.get("tasks"):
        return [event("status", {"stage": "plan", "tasks": [{"question": t["question"], "source": t["source_name"]}
                                                            for t in update["tasks"]]})]
    if node == "research" and update.get("findings"):
        f = update["findings"][0]
        return [event("status", {"stage": "research", "question": f["question"], "source": f["source_name"],
                                 "found": f["answered"]})]
    if node == "review" and "rounds" in update:
        result = "more_research" if update.get("tasks") else "rewrite" if "feedback" in update else "approved"
        return [event("status", {"stage": "review", "result": result})]
    return []


async def final_event(graph, config) -> ServerSentEvent:
    state = (await graph.aget_state(config)).values
    turn = current_turn(state["messages"])
    answer = next((m.text for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), "")
    sources = sources_in(turn)
    return event("done", {"answer": answer, "sources": sources,
                          "guard": (state.get("guard") or "ok").split(":")[0]})


async def stream_turn(graph, config, graph_input, lock: asyncio.Lock, on_end):
    """Run one turn (or the rest of a paused one) and yield its events.

    on_end(tokens, result) runs however the turn ends, to record usage and audit it.
    """
    usage = UsageMetadataCallbackHandler()  # counts tokens of every model call in the turn
    config = {**config, "callbacks": [usage]}
    result = "done"
    async with lock:  # one turn at a time per conversation
        try:
            # Sent at once: the input check alone takes 1-7 s, and silence feels like a hang.
            yield event("status", {"stage": "received"})
            interrupted = None
            try:
                async with asyncio.timeout(TURN_TIMEOUT_S):
                    async for _ns, mode, data in graph.astream(graph_input, config, subgraphs=True,
                                                               stream_mode=["updates", "messages"]):
                        if mode == "messages":
                            chunk, meta = data
                            # Only the answering model; not the guard, grader or fact checker.
                            if meta.get("langgraph_node") in ANSWER_NODES and isinstance(chunk, AIMessageChunk) \
                                    and chunk.text:
                                yield event("token", {"message_id": chunk.id, "text": chunk.text})
                            continue
                        for node, update in data.items():
                            if node == "__interrupt__":
                                interrupted = update[0].value
                            else:
                                for e in describe(node, update):
                                    yield e
            except asyncio.CancelledError:
                # The client went away mid-turn. Leave the conversation valid, then stop.
                result = "cancelled"
                await asyncio.shield(step9_agent.recover(graph, config, ConnectionAbortedError("client left")))
                raise
            except Exception as e:  # noqa: BLE001 - timeout, model down, step limit: repair and report
                log.warning("turn failed: %s: %s", type(e).__name__, e)
                result = f"error: {type(e).__name__}"
                await step9_agent.recover(graph, config, e)
                yield event("error", {"type": type(e).__name__})
            if interrupted is not None:
                result = "approval"
                yield event("approval", {"tool_calls": [{"id": c["id"], "name": c["name"], "args": c["args"]}
                                                        for c in interrupted["tool_calls"]]})
                return
            yield await final_event(graph, config)
        finally:
            tokens = sum(u.get("total_tokens", 0) for u in usage.usage_metadata.values())
            try:
                await asyncio.shield(on_end(tokens, result))
            except Exception as e:  # noqa: BLE001 - bookkeeping must never break the reply
                log.error("could not record the turn: %s", e)


def turn_recorder(request: Request, user: User, graph, config, bot_id: str, thread_id: str):
    auth = request.app.state.auth

    async def on_end(tokens: int, result: str):
        state = (await graph.aget_state(config)).values
        await auth.add_usage(user.username, turns=0, tokens=tokens)
        # Who, what and how much; never the message text.
        await auth.audit(user.username, "turn.end", bot_id=bot_id, thread_id=thread_id, ip=client_ip(request),
                         detail={"result": result, "tokens": tokens,
                                 "guard": (state.get("guard") or "ok").split(":")[0]})

    return on_end


# ------------------------------------------------------------------ routes: sign-in

@app.post("/auth/login")
async def login(request: Request, response: Response, body: LoginIn):
    auth, ip = request.app.state.auth, client_ip(request)
    limiter, key = request.app.state.login_limiter, f"{ip}|{body.username}"
    wait = limiter.retry_after(key, record=False)  # only failures count, so signing in often is fine
    if wait:  # slows down password guessing
        await auth.audit(body.username[:64], "auth.login_blocked", ip=ip)
        raise HTTPException(429, "Too many sign-in attempts. Try again in a minute.",
                            headers={"Retry-After": str(wait)})
    user = await auth.authenticate(body.username, body.password)
    if user is None:
        limiter.retry_after(key)  # count the failure
        await auth.audit(body.username[:64], "auth.login_failed", ip=ip)
        raise HTTPException(401, "Wrong username or password.")  # never says which one
    token = await auth.start_session(user.username)
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, path="/")
    await auth.audit(user.username, "auth.login", ip=ip)
    return {"username": user.username, "role": user.role, "usage": await auth.usage_today(user.username)}


@app.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response, user: User = Depends(current_user)):
    await request.app.state.auth.end_session(request.cookies[COOKIE_NAME])
    response.delete_cookie(COOKIE_NAME, path="/")
    await request.app.state.auth.audit(user.username, "auth.logout", ip=client_ip(request))


@app.get("/auth/me")
async def me(request: Request, user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role,
            "usage": await request.app.state.auth.usage_today(user.username)}


# ------------------------------------------------------------------ routes: bots and conversations

@app.get("/health")
async def health(request: Request):
    return {"status": "ok", "bots": len(request.app.state.platform.bots)}


@app.get("/bots", response_model=list[BotPublic])
async def list_bots(request: Request, user: User = Depends(current_user)):
    auth = request.app.state.auth
    return [BotPublic(**e.config.model_dump()) for bot_id, e in request.app.state.platform.bots.items()
            if await auth.can_use(user, bot_id)]


@app.get("/bots/{bot_id}", response_model=BotPublic)
async def get_bot_info(request: Request, bot_id: str, user: User = Depends(current_user)):
    return BotPublic(**(await bot_for(request, user, bot_id)).config.model_dump())


@app.get("/bots/{bot_id}/threads")
async def list_threads(request: Request, bot_id: str, user: User = Depends(current_user)):
    await bot_for(request, user, bot_id)
    return await request.app.state.auth.threads_for(user.username, bot_id)


@app.post("/bots/{bot_id}/threads", status_code=201)
async def new_thread(request: Request, bot_id: str, user: User = Depends(current_user)):
    entry = await bot_for(request, user, bot_id)
    thread_id = uuid.uuid4().hex
    await request.app.state.auth.add_thread(thread_id, bot_id, user.username)
    return {"thread_id": thread_id, "welcome": entry.config.welcome}


@app.get("/bots/{bot_id}/threads/{thread_id}", response_model=ThreadOut)
async def get_thread(request: Request, bot_id: str, thread_id: str, user: User = Depends(current_user)):
    entry = await bot_for(request, user, bot_id)
    await own_thread(request, user, bot_id, thread_id)
    graph = await request.app.state.platform.graph_for(entry, user)
    config = await config_for(request, user, entry, thread_id)
    state = (await graph.aget_state(config)).values
    messages, turn = [], []  # turn: the tool results since the last user message
    for m in state.get("messages", []):
        if isinstance(m, HumanMessage):
            messages.append(ChatMessage(role="user", text=m.text))
            turn = []
        elif isinstance(m, ToolMessage):
            turn.append(m)
        elif isinstance(m, AIMessage) and not m.tool_calls and m.text:
            messages.append(ChatMessage(role="assistant", text=m.text, sources=sources_in(turn + [m])))
    return ThreadOut(thread_id=thread_id, messages=messages,
                     pending_approval=await pending_approval(graph, config))


@app.post("/bots/{bot_id}/threads/{thread_id}/messages")
async def send_message(request: Request, bot_id: str, thread_id: str, body: MessageIn,
                       user: User = Depends(current_user)):
    entry, platform, auth = await bot_for(request, user, bot_id), request.app.state.platform, request.app.state.auth
    await own_thread(request, user, bot_id, thread_id)
    rate_limit(request, user)
    await check_quota(request, user, bot_id)
    graph = await platform.graph_for(entry, user)
    config = await config_for(request, user, entry, thread_id)
    lock = platform.thread_locks[f"{bot_id}:{thread_id}"]
    if lock.locked():
        raise HTTPException(409, "A reply in this conversation is still being written.")
    if await pending_approval(graph, config):
        raise HTTPException(409, "This conversation is waiting for an approval decision.")
    # Counted when it starts, in one conditional write, so parallel conversations
    # cannot overrun the turn quota (check_quota above is only the early, cheap check).
    problem = await auth.reserve_turn(user.username)
    if problem:
        await auth.audit(user.username, "quota.exceeded", bot_id=bot_id, ip=client_ip(request))
        raise HTTPException(429, problem)
    await auth.audit(user.username, "turn.start", bot_id=bot_id, thread_id=thread_id, ip=client_ip(request),
                     detail={"chars": len(body.text)})
    graph_input = {"messages": [HumanMessage(body.text)]}
    on_end = turn_recorder(request, user, graph, config, bot_id, thread_id)
    return EventSourceResponse(stream_turn(graph, config, graph_input, lock, on_end), ping=15)


@app.post("/bots/{bot_id}/threads/{thread_id}/approval")
async def send_approval(request: Request, bot_id: str, thread_id: str, body: ApprovalIn,
                        user: User = Depends(current_user)):
    entry, platform, auth = await bot_for(request, user, bot_id), request.app.state.platform, request.app.state.auth
    await own_thread(request, user, bot_id, thread_id)
    rate_limit(request, user)
    graph = await platform.graph_for(entry, user)
    config = await config_for(request, user, entry, thread_id)
    lock = platform.thread_locks[f"{bot_id}:{thread_id}"]
    if lock.locked():
        raise HTTPException(409, "A reply in this conversation is still being written.")
    waiting = await pending_approval(graph, config)
    if not waiting:
        raise HTTPException(409, "Nothing in this conversation is waiting for approval.")
    # Who approved what: the tools and the decision (the arguments are the user's own data).
    await auth.audit(user.username, "approval.decision", bot_id=bot_id, thread_id=thread_id,
                     ip=client_ip(request), detail={"decision": body.decision,
                                                    "tools": [c["name"] for c in waiting["tool_calls"]]})
    on_end = turn_recorder(request, user, graph, config, bot_id, thread_id)
    return EventSourceResponse(stream_turn(graph, config, Command(resume=body.decision), lock, on_end), ping=15)


# ------------------------------------------------------------------ routes: admin

@app.get("/admin/audit")
async def audit_log(request: Request, limit: int = 50, user: User = Depends(admin_user)):
    auth = request.app.state.auth
    intact, rows, broken_at = await auth.verify_audit()
    return {"chain": {"intact": intact, "rows": rows, "broken_at": broken_at},
            "entries": await auth.audit_entries(min(max(limit, 1), 500))}


# ------------------------------------------------------------------ routes: building bots (Step 15)

def build_for(request: Request, build_id: str) -> Build:
    build = request.app.state.builds.builds.get(build_id)
    if build is None:
        raise HTTPException(404, "No such build")
    return build


def build_detail(build: Build) -> dict:
    return {**build.summary(), "events": build.events, "proposal": build.proposal,
            "problems": build.problems, "saved": build.saved}


@app.post("/admin/builds", status_code=201)
async def start_build(request: Request, user: User = Depends(admin_user)):
    # The form is read here, after the admin check, so nobody else can make the server
    # store large uploads. The declared size is checked before a byte is read.
    builds: BuildManager = request.app.state.builds
    if int(request.headers.get("content-length") or 0) > MAX_TOTAL_BYTES + 2**20:
        raise HTTPException(413, f"The upload is larger than {MAX_TOTAL_BYTES // 2**20} MB.")
    if builds.busy():
        raise HTTPException(409, "Another bot is being built. Try again when it has finished.")
    async with request.form(max_files=MAX_FILES + 1, max_fields=10) as form:
        bot_id, use_case = str(form.get("bot_id") or "").strip(), str(form.get("use_case") or "")
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
        try:
            builds.check_request(bot_id, use_case)
            # Read at most one byte over the limit: enough to know a file is too large.
            files = [(f.filename or "file", await f.read(MAX_FILE_BYTES + 1)) for f in uploads]
            folder, names = builds.store_files(bot_id, files)
        except UploadProblem as e:
            raise HTTPException(400, str(e)) from None
    build = await builds.start(bot_id, use_case, folder, names, by=user.username)
    await request.app.state.auth.audit(user.username, "build.start", bot_id=bot_id, ip=client_ip(request),
                                       detail={"build": build.id, "files": len(names),
                                               "bytes": sum(len(d) for _, d in files)})
    return build.summary()


@app.get("/admin/builds")
async def list_builds(request: Request, user: User = Depends(admin_user)):
    return sorted((b.summary() for b in request.app.state.builds.builds.values()),
                  key=lambda b: b["started"], reverse=True)


@app.get("/admin/builds/{build_id}")
async def get_build(request: Request, build_id: str, user: User = Depends(admin_user)):
    return build_detail(build_for(request, build_id))


@app.get("/admin/builds/{build_id}/events")
async def build_events(request: Request, build_id: str, after: int = 0, user: User = Depends(admin_user)):
    build = build_for(request, build_id)
    # EventSource reconnects by itself and sends the last id it saw: continue from there.
    last = request.headers.get("last-event-id")
    after = int(last) if last and last.isdigit() else max(after, 0)

    async def events():
        async for e in request.app.state.builds.follow(build, after):
            yield ServerSentEvent(id=str(e["n"]), event="build", data=json.dumps(e, ensure_ascii=False))
        yield ServerSentEvent(event="end", data=json.dumps({"status": build.status}))

    return EventSourceResponse(events(), ping=15)


@app.post("/admin/builds/{build_id}/decision")
async def decide_build(request: Request, build_id: str, body: DecisionIn, user: User = Depends(admin_user)):
    build = build_for(request, build_id)
    try:
        await request.app.state.builds.decide(build, body.decision)
    except UploadProblem as e:
        raise HTTPException(409, str(e)) from None
    await request.app.state.auth.audit(user.username, "build.decision", bot_id=build.bot_id, ip=client_ip(request),
                                       detail={"build": build.id, "decision": body.decision,
                                               "result": build.status})
    return build_detail(build)


# ------------------------------------------------------------------ routes: error analysis (Step 17)
# A domain expert labels a bot's conversations here; the LLM steps run from analysis.py.

class LabelIn(BaseModel):
    verdict: Literal["pass", "fail"]
    note: str = Field("", max_length=analysis.MAX_NOTE)
    first_failure: Literal[analysis.STEPS] | None = None


class ModesIn(BaseModel):
    modes: list[str] = Field(default_factory=list, max_length=10)


def workspace_for(bot_id: str) -> analysis.Workspace:
    try:
        ws = analysis.Workspace(bot_id)
    except ValueError:
        raise HTTPException(404, "No analysis for this bot") from None
    if not ws.exists():
        raise HTTPException(404, "No analysis for this bot")
    return ws


@app.get("/admin/analysis")
async def list_analyses(user: User = Depends(admin_user)):
    return [ws.summary() for ws in analysis.workspaces()]


@app.get("/admin/analysis/{bot_id}")
async def get_analysis(bot_id: str, user: User = Depends(admin_user)):
    return workspace_for(bot_id).detail()


@app.put("/admin/analysis/{bot_id}/labels/{trace_id}")
async def save_label(request: Request, bot_id: str, trace_id: str, body: LabelIn, user: User = Depends(admin_user)):
    ws = workspace_for(bot_id)
    try:
        label = ws.save_label(trace_id, body.verdict, body.note, body.first_failure, by=user.username)
    except KeyError:
        raise HTTPException(404, "No such conversation") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    await request.app.state.auth.audit(user.username, "analysis.label", bot_id=bot_id, ip=client_ip(request),
                                       detail={"trace": trace_id, "verdict": body.verdict})
    return {"label": label, "summary": ws.summary(), "taxonomy": ws.taxonomy() or None}


@app.put("/admin/analysis/{bot_id}/assignments/{trace_id}")
async def assign_modes(request: Request, bot_id: str, trace_id: str, body: ModesIn,
                       user: User = Depends(admin_user)):
    ws = workspace_for(bot_id)
    try:
        taxonomy = ws.set_assignment(trace_id, body.modes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    await request.app.state.auth.audit(user.username, "analysis.assign", bot_id=bot_id, ip=client_ip(request),
                                       detail={"trace": trace_id, "modes": body.modes})
    return taxonomy


@app.post("/admin/analysis/{bot_id}/taxonomy/approve")
async def approve_taxonomy(request: Request, bot_id: str, user: User = Depends(admin_user)):
    ws = workspace_for(bot_id)
    try:
        taxonomy = ws.approve_taxonomy(by=user.username)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    await request.app.state.auth.audit(user.username, "analysis.approve", bot_id=bot_id, ip=client_ip(request),
                                       detail={"modes": [m["id"] for m in taxonomy["modes"]]})
    return taxonomy


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # Still localhost only: sessions travel in a cookie that needs HTTPS (API_COOKIE_SECURE=true)
    # before this may listen on a network.
    # On Windows, "none" makes uvicorn use the Selector loop engine.db asks for (uvicorn's
    # own Windows default, Proactor, can't run psycopg in async mode). Linux keeps uvicorn's default.
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("API_PORT", "8000")),
                loop="none" if sys.platform == "win32" else "auto")
