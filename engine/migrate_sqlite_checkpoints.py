"""Lampus Phase 1, Step 4 - Copy saved conversations (LangGraph checkpoints) from SQLite to Postgres.

    python -m engine.migrate_sqlite_checkpoints                        # data/checkpoints.sqlite -> DATABASE_URL
    python -m engine.migrate_sqlite_checkpoints --sqlite other.sqlite

Copies every checkpoint of every conversation, oldest first so each one's parent
already exists, together with its pending writes (for example a tool call
waiting for approval). Conversation ownership moved with the users in Step 3
(python -m engine.migrate_sqlite_auth). Safe to run again: a conversation
already in Postgres is skipped.
"""

import argparse
import asyncio
from collections import defaultdict
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from engine.db import postgres_checkpointer
from engine.step5_agent import CHECKPOINT_DB


def summary(tup) -> int:
    return len((tup.checkpoint.get("channel_values") or {}).get("messages", []))


async def migrate(sqlite_path: Path):
    async with AsyncSqliteSaver.from_conn_string(str(sqlite_path)) as src, postgres_checkpointer() as dst:
        by_thread = defaultdict(list)
        async for tup in src.alist(None):
            by_thread[(tup.config["configurable"]["thread_id"], tup.config["configurable"].get("checkpoint_ns", ""))
                      ].append(tup)
        copied = skipped = checkpoints = 0
        for (thread, ns), tuples in by_thread.items():
            if await dst.aget_tuple({"configurable": {"thread_id": thread, "checkpoint_ns": ns}}):
                skipped += 1
                continue
            for tup in sorted(tuples, key=lambda t: t.checkpoint["id"]):  # ids are time-ordered
                parent = tup.parent_config or {"configurable": {"thread_id": thread, "checkpoint_ns": ns}}
                cfg = await dst.aput(parent, tup.checkpoint, tup.metadata, tup.checkpoint["channel_versions"])
                writes = defaultdict(list)
                for task_id, channel, value in tup.pending_writes or []:
                    writes[task_id].append((channel, value))
                for task_id, items in writes.items():
                    await dst.aput_writes(cfg, items, task_id)
                checkpoints += 1
            copied += 1

        # Check: every copied conversation's latest state has the same messages as before.
        mismatches = 0
        for (thread, ns), tuples in by_thread.items():
            cfg = {"configurable": {"thread_id": thread, "checkpoint_ns": ns}}
            before, after = await src.aget_tuple(cfg), await dst.aget_tuple(cfg)
            if after is None or summary(before) != summary(after) or before.checkpoint["id"] != after.checkpoint["id"]:
                mismatches += 1
        print(f"Copied {copied} conversations ({checkpoints} checkpoints); skipped {skipped} already in Postgres.")
        print(f"Latest state matches in {len(by_thread) - mismatches} of {len(by_thread)} conversations.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", type=Path, default=CHECKPOINT_DB)
    args = parser.parse_args()
    if not args.sqlite.exists():
        raise SystemExit(f"No SQLite checkpoints at {args.sqlite}")
    asyncio.run(migrate(args.sqlite))


if __name__ == "__main__":
    main()
