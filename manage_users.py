"""Step 13 - Manage users, bot access and the audit log from the command line.

    python manage_users.py add-user alice              # asks for a password (member)
    python manage_users.py add-user admin --role admin
    python manage_users.py grant alice study_assistant
    python manage_users.py revoke alice study_assistant
    python manage_users.py users
    python manage_users.py passwd alice                # also signs alice out everywhere
    python manage_users.py disable alice               # / enable alice
    python manage_users.py audit --limit 30            # latest events
    python manage_users.py audit --verify              # check the hash chain

Passwords are asked for without echo, never taken as arguments (arguments end
up in shell history and process lists). --password-stdin reads one line
instead, for scripts.
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from auth import AuthStore
from bot_config import load_bot

BOTS_DIR = Path(__file__).parent / "bots"


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        sys.exit("Passwords do not match.")
    return first


def known_bots() -> set[str]:
    return {load_bot(p).id for p in BOTS_DIR.glob("*.yaml")}


async def run(args):
    store = await AuthStore().open()
    try:
        if args.command == "add-user":
            await store.create_user(args.username, read_password(args.password_stdin), args.role)
            await store.audit("cli", "user.create", detail={"username": args.username, "role": args.role})
            print(f"Created {args.role} '{args.username}'.")
        elif args.command == "passwd":
            await store.set_password(args.username, read_password(args.password_stdin))
            await store.audit("cli", "user.password", detail={"username": args.username})
            print(f"Password changed; '{args.username}' was signed out everywhere.")
        elif args.command in ("disable", "enable"):
            await store.set_disabled(args.username, args.command == "disable")
            await store.audit("cli", f"user.{args.command}", detail={"username": args.username})
            print(f"'{args.username}' {args.command}d.")
        elif args.command in ("grant", "revoke"):
            if args.command == "grant" and args.bot not in known_bots():
                sys.exit(f"No bot '{args.bot}'. Bots: {', '.join(sorted(known_bots()))}")
            await getattr(store, args.command)(args.username, args.bot)
            await store.audit("cli", f"access.{args.command}", bot_id=args.bot, detail={"username": args.username})
            print(f"{args.command.capitalize()}ed '{args.bot}' for '{args.username}'.")
        elif args.command == "users":
            for u in await store.list_users():
                state = " (disabled)" if u["disabled"] else ""
                bots = "all bots" if u["role"] == "admin" else (", ".join(u["bots"]) or "no bots")
                print(f"{u['username']:20} {u['role']:7} {bots}{state}")
        elif args.command == "audit":
            if args.verify:
                ok, count, bad = await store.verify_audit()
                print(f"Audit log intact: {count} rows." if ok else
                      f"Audit log BROKEN at row {bad} (after {count} intact rows): a row was changed or removed.")
                sys.exit(0 if ok else 1)
            for e in reversed(await store.audit_entries(args.limit)):
                where = " ".join(x for x in (e["bot_id"], (e["thread_id"] or "")[:8]) if x)
                print(f"{e['ts']}  {e['actor']:14} {e['action']:20} {where:28} {e['detail'] or ''}")
    except ValueError as e:
        sys.exit(str(e))
    finally:
        await store.close()


def main():
    parser = argparse.ArgumentParser(description="Users, bot access and the audit log.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("add-user")
    p.add_argument("username")
    p.add_argument("--role", default="member", choices=["member", "admin"])
    p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("passwd")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    for name in ("grant", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("bot")
    sub.add_parser("users")
    p = sub.add_parser("audit")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--verify", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
