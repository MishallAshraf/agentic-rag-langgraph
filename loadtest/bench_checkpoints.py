"""Benchmark the conversation store: SQLite file vs Postgres pool, under concurrent conversations.

    python loadtest/bench_checkpoints.py --backend sqlite
    python loadtest/bench_checkpoints.py --backend postgres      # uses DATABASE_URL

Every chat turn reads the conversation's latest checkpoint, runs, and saves new
checkpoints (LangGraph saves one per step). This repeats that pattern without a
model: N conversations at once, each doing 10 turns of "read latest, save 4 new"
while its history grows like a real chat (about 1.5 kB of text per message,
illustrative). It measures checkpoint operations per second and how long one
turn's checkpoint work takes (p50 / p95).

The SQLite saver uses one connection with a lock, so concurrent conversations
queue behind each other; the Postgres saver borrows connections from a pool.
Results are appended to loadtest/results.jsonl.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import engine.db  # noqa: E402,F401  (before any event loop: switches Windows to the Selector loop)

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.base import empty_checkpoint  # noqa: E402

RESULTS = Path(__file__).parent / "results.jsonl"
TURNS = 10
STEPS_PER_TURN = 4  # checkpoints saved per turn (one per graph step; illustrative)
TEXT = "This is a realistic tutoring message about fractions and how to compare them. " * 20  # ~1.5 kB


def make_checkpoint(messages: list, step: int) -> tuple[dict, dict]:
    cp = empty_checkpoint()
    cp["id"] = str(uuid.uuid6()) if hasattr(uuid, "uuid6") else _uuid6()
    version = f"{step:032}.0"
    cp["channel_values"] = {"messages": list(messages)}
    cp["channel_versions"] = {"messages": version}
    return cp, {"messages": version}


def _uuid6() -> str:
    from langgraph.checkpoint.base.id import uuid6
    return str(uuid6())


async def conversation(saver, timings: list):
    thread = uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    messages, step = [], 0
    for _ in range(TURNS):
        start = time.perf_counter()
        latest = await saver.aget_tuple(config)
        if latest:
            config = latest.config
        messages += [HumanMessage(TEXT), AIMessage(TEXT)]
        for _ in range(STEPS_PER_TURN):
            step += 1
            cp, versions = make_checkpoint(messages, step)
            config = await saver.aput(config, cp, {"source": "loop", "step": step}, versions)
        timings.append(time.perf_counter() - start)


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


async def run(backend: str, concurrency: list[int]):
    if backend == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        path = ROOT / "data" / "loadtest" / "bench_checkpoints.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        ctx = AsyncSqliteSaver.from_conn_string(str(path))
        saver = await ctx.__aenter__()
        close = lambda: ctx.__aexit__(None, None, None)  # noqa: E731
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        pool = await engine.db.open_pool()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        close = pool.close
    try:
        for n in concurrency:
            timings: list[float] = []
            start = time.perf_counter()
            await asyncio.gather(*(conversation(saver, timings) for _ in range(n)))
            wall = time.perf_counter() - start
            ops = n * TURNS * (1 + STEPS_PER_TURN)
            summary = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "label": f"checkpoint bench: {backend}", "conversations": n,
                       "ops_per_s": round(ops / wall), "turn_store_p50_ms": round(pct(timings, 50) * 1000, 1),
                       "turn_store_p95_ms": round(pct(timings, 95) * 1000, 1), "wall_s": round(wall, 2)}
            print(json.dumps(summary))
            with RESULTS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(summary) + "\n")
    finally:
        await close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["sqlite", "postgres"], required=True)
    parser.add_argument("--concurrency", default="1,10,50")
    args = parser.parse_args()
    asyncio.run(run(args.backend, [int(x) for x in args.concurrency.split(",")]))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main()
