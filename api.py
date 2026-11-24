"""Step 11 - The agent behind an HTTP API, streaming over Server-Sent Events.

Until now each agent ran in a terminal. A web frontend (Step 12) needs an API:

    GET  /health                                   is the server up, how many bots
    GET  /bots                                     the bots, public fields only
    GET  /bots/{bot}                               one bot (never its system prompt)
    POST /bots/{bot}/threads                       start a conversation -> thread_id
    GET  /bots/{bot}/threads/{thread}              its messages, and a pending approval if any
    POST /bots/{bot}/threads/{thread}/messages     send a message; the reply streams back (SSE)
    POST /bots/{bot}/threads/{thread}/approval     approve or reject a paused tool call (SSE)

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

    python api.py          # http://127.0.0.1:8000, docs at /docs

There is no authentication yet (Step 13), so the server listens on localhost
only, and thread ids are long random values that cannot be guessed.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

import step9_agent
from bot_config import BotConfig, Theme, load_bot
from step5_agent import CHECKPOINT_DB
from step7_agent import SOURCE_TOOLS, current_turn
from tool_registry import load_tools

load_dotenv()  # before the API_* settings below are read
log = logging.getLogger("api")

BOTS_DIR = Path(__file__).parent / "bots"
TURN_TIMEOUT_S = float(os.getenv("API_TURN_TIMEOUT", "120"))  # latency budget for one turn
RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "10"))            # turns per client per window
RATE_WINDOW_S = 60
MAX_MESSAGE_CHARS = 4000
CORS_ORIGINS = os.getenv("API_CORS_ORIGINS", "http://localhost:5173").split(",")  # Vite dev server
THREAD_PATTERN = re.compile(r"^[a-f0-9]{32}$")
SOURCE_PATTERN = re.compile(r"^\[\d+\] (.+)$", re.MULTILINE)  # "[1] file, page 4" in search results


# ------------------------------------------------------------------ bots and their graphs

@dataclass
class BotEntry:
    config: BotConfig
    graph: object = None
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

    async def graph_for(self, entry: BotEntry):
        # Built on first use: loading tools can start an MCP server process.
        async with entry.build_lock:
            if entry.graph is None:
                tools = await load_tools(entry.config.tools, entry.config)  # enforces the tool policy
                entry.graph = step9_agent.build_graph(entry.config, tools, self.llm, self.checkpointer)
        return entry.graph


class RateLimiter:
    """Sliding window per client: at most `limit` requests in `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def retry_after(self, client: str) -> int:
        """0 if the request may go ahead (and record it), else seconds to wait."""
        now, hits = time.monotonic(), self.hits[client]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return int(self.window - (now - hits[0])) + 1
        hits.append(now)
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    from llm import get_llm

    CHECKPOINT_DB.parent.mkdir(exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as checkpointer:
        app.state.platform = Platform(checkpointer, get_llm())
        app.state.platform.load_bots()
        app.state.limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
        yield


app = FastAPI(title="Agentic RAG bots", version="0.11.0", lifespan=lifespan)
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


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ApprovalIn(BaseModel):
    decision: Literal["approve", "reject"]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    sources: list[str] = []  # for an answer: what its turn retrieved


class ThreadOut(BaseModel):
    thread_id: str
    messages: list[ChatMessage]
    pending_approval: dict | None = None


# ------------------------------------------------------------------ helpers

def get_bot(request: Request, bot_id: str) -> BotEntry:
    entry = request.app.state.platform.bots.get(bot_id)
    if entry is None:
        raise HTTPException(404, f"No bot '{bot_id}'")
    return entry


def check_thread_id(thread_id: str):
    if not THREAD_PATTERN.fullmatch(thread_id):
        raise HTTPException(404, "No such conversation")


def rate_limit(request: Request):
    client = request.client.host if request.client else "unknown"
    wait = request.app.state.limiter.retry_after(client)
    if wait:
        raise HTTPException(429, "Too many messages; slow down.", headers={"Retry-After": str(wait)})


async def pending_approval(graph, config) -> dict | None:
    snapshot = await graph.aget_state(config, subgraphs=True)
    interrupts = list(snapshot.interrupts) + [i for t in snapshot.tasks for i in t.interrupts]
    return interrupts[0].value if interrupts else None


def sources_in(messages) -> list[str]:
    return sorted({s.strip() for m in messages if isinstance(m, ToolMessage) and m.name in SOURCE_TOOLS
                   for s in SOURCE_PATTERN.findall(m.text)})


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
    return []


async def final_event(graph, config) -> ServerSentEvent:
    state = (await graph.aget_state(config)).values
    turn = current_turn(state["messages"])
    answer = next((m.text for m in reversed(turn) if isinstance(m, AIMessage) and not m.tool_calls), "")
    sources = sources_in(turn)
    return event("done", {"answer": answer, "sources": sources,
                          "guard": (state.get("guard") or "ok").split(":")[0]})


async def stream_turn(platform: Platform, graph, config, graph_input, lock: asyncio.Lock):
    """Run one turn (or the rest of a paused one) and yield its events."""
    async with lock:  # one turn at a time per conversation
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
                        if meta.get("langgraph_node") == "agent" and isinstance(chunk, AIMessageChunk) \
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
            await asyncio.shield(step9_agent.recover(graph, config, ConnectionAbortedError("client left")))
            raise
        except Exception as e:  # noqa: BLE001 - timeout, model down, step limit: repair and report
            log.warning("turn failed: %s: %s", type(e).__name__, e)
            await step9_agent.recover(graph, config, e)
            yield event("error", {"type": type(e).__name__})
        if interrupted is not None:
            yield event("approval", {"tool_calls": [{"id": c["id"], "name": c["name"], "args": c["args"]}
                                                    for c in interrupted["tool_calls"]]})
            return
        yield await final_event(graph, config)


# ------------------------------------------------------------------ routes

@app.get("/health")
async def health(request: Request):
    return {"status": "ok", "bots": len(request.app.state.platform.bots)}


@app.get("/bots", response_model=list[BotPublic])
async def list_bots(request: Request):
    return [BotPublic(**e.config.model_dump()) for e in request.app.state.platform.bots.values()]


@app.get("/bots/{bot_id}", response_model=BotPublic)
async def get_bot_info(request: Request, bot_id: str):
    return BotPublic(**get_bot(request, bot_id).config.model_dump())


@app.post("/bots/{bot_id}/threads", status_code=201)
async def new_thread(request: Request, bot_id: str):
    entry = get_bot(request, bot_id)
    return {"thread_id": uuid.uuid4().hex, "welcome": entry.config.welcome}


@app.get("/bots/{bot_id}/threads/{thread_id}", response_model=ThreadOut)
async def get_thread(request: Request, bot_id: str, thread_id: str):
    entry, platform = get_bot(request, bot_id), request.app.state.platform
    check_thread_id(thread_id)
    graph = await platform.graph_for(entry)
    config = step9_agent.turn_config(entry.config, thread_id)
    state = (await graph.aget_state(config)).values
    messages, turn = [], []  # turn: the tool results since the last user message
    for m in state.get("messages", []):
        if isinstance(m, HumanMessage):
            messages.append(ChatMessage(role="user", text=m.text))
            turn = []
        elif isinstance(m, ToolMessage):
            turn.append(m)
        elif isinstance(m, AIMessage) and not m.tool_calls and m.text:
            messages.append(ChatMessage(role="assistant", text=m.text, sources=sources_in(turn)))
    return ThreadOut(thread_id=thread_id, messages=messages,
                     pending_approval=await pending_approval(graph, config))


@app.post("/bots/{bot_id}/threads/{thread_id}/messages")
async def send_message(request: Request, bot_id: str, thread_id: str, body: MessageIn):
    entry, platform = get_bot(request, bot_id), request.app.state.platform
    check_thread_id(thread_id)
    rate_limit(request)
    graph = await platform.graph_for(entry)
    config = step9_agent.turn_config(entry.config, thread_id)
    lock = platform.thread_locks[f"{bot_id}:{thread_id}"]
    if lock.locked():
        raise HTTPException(409, "A reply in this conversation is still being written.")
    if await pending_approval(graph, config):
        raise HTTPException(409, "This conversation is waiting for an approval decision.")
    graph_input = {"messages": [HumanMessage(body.text)]}
    return EventSourceResponse(stream_turn(platform, graph, config, graph_input, lock), ping=15)


@app.post("/bots/{bot_id}/threads/{thread_id}/approval")
async def send_approval(request: Request, bot_id: str, thread_id: str, body: ApprovalIn):
    entry, platform = get_bot(request, bot_id), request.app.state.platform
    check_thread_id(thread_id)
    rate_limit(request)
    graph = await platform.graph_for(entry)
    config = step9_agent.turn_config(entry.config, thread_id)
    lock = platform.thread_locks[f"{bot_id}:{thread_id}"]
    if lock.locked():
        raise HTTPException(409, "A reply in this conversation is still being written.")
    if not await pending_approval(graph, config):
        raise HTTPException(409, "Nothing in this conversation is waiting for approval.")
    return EventSourceResponse(stream_turn(platform, graph, config, Command(resume=body.decision), lock),
                               ping=15)


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # Localhost only until Step 13 adds authentication.
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("API_PORT", "8000")))
