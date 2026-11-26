"""Lampus Phase 1, Step 3 - Copy users, grants, conversations, usage and the audit log from SQLite to Postgres.

    python -m engine.migrate_sqlite_auth                       # data/auth.sqlite -> DATABASE_URL
    python -m engine.migrate_sqlite_auth --sqlite other.sqlite

Run once, into an empty Postgres database. Sessions are not copied: everyone
signs in again. Audit rows keep their exact text and hashes, in the same order,
so `python -m engine.manage_users audit --verify` still checks the whole chain,
including the rows written before the move.
"""

import argparse
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from engine.auth import AuthStore

ROOT = Path(__file__).resolve().parent.parent


def created(value: str):
    return datetime.fromisoformat(value) if value else datetime.now(timezone.utc)


async def migrate(sqlite_path: Path):
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    store = await AuthStore().open()
    try:
        async with store.pool.connection() as conn, conn.transaction():  # all or nothing
            if (await (await conn.execute("SELECT count(*) AS n FROM users")).fetchone())["n"]:
                raise SystemExit("Postgres already has users; this copies into an empty database only.")
            users = src.execute("SELECT * FROM users").fetchall()
            for u in users:
                await conn.execute("INSERT INTO users VALUES (%s, %s, %s, %s, %s)",
                                   (u["username"], u["password_hash"], u["role"], bool(u["disabled"]),
                                    created(u["created_at"])))
            grants = src.execute("SELECT * FROM grants").fetchall()
            for g in grants:
                await conn.execute("INSERT INTO grants VALUES (%s, %s)", (g["username"], g["bot_id"]))
            threads = src.execute("SELECT * FROM threads").fetchall()
            for t in threads:
                await conn.execute("INSERT INTO threads VALUES (%s, %s, %s, %s)",
                                   (t["thread_id"], t["bot_id"], t["username"], created(t["created_at"])))
            usage = src.execute("SELECT * FROM usage").fetchall()
            for r in usage:
                await conn.execute("INSERT INTO usage VALUES (%s, %s, %s, %s)",
                                   (r["username"], r["day"], r["turns"], r["tokens"]))
            audit = src.execute("SELECT * FROM audit ORDER BY id").fetchall()
            for a in audit:
                await conn.execute(
                    "INSERT INTO audit (ts, actor, action, bot_id, thread_id, detail, ip, prev_hash, hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (a["ts"], a["actor"], a["action"], a["bot_id"], a["thread_id"], a["detail"], a["ip"],
                     a["prev_hash"], a["hash"]))
        intact, rows, broken_at = await store.verify_audit()
        print(f"Copied {len(users)} users, {len(grants)} grants, {len(threads)} conversations, "
              f"{len(usage)} usage rows, {len(audit)} audit rows.")
        print(f"Audit chain after the move: {'intact' if intact else f'BROKEN at row {broken_at}'} ({rows} rows).")
        await store.audit("cli", "store.migrated", detail={"from": "sqlite", "users": len(users),
                                                            "audit_rows": len(audit)})
    finally:
        await store.close()
        src.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", type=Path, default=ROOT / "data" / "auth.sqlite")
    args = parser.parse_args()
    if not args.sqlite.exists():
        raise SystemExit(f"No SQLite database at {args.sqlite}")
    asyncio.run(migrate(args.sqlite))


if __name__ == "__main__":
    main()
