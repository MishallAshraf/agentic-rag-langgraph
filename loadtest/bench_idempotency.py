"""Does a resent message run twice? (Lampus Phase 1, Step 10)

    python loadtest/bench_idempotency.py --url http://127.0.0.1:8001

Phones lose connections. A student's app sends a message, the answer comes
back, but the connection drops before the app sees the end of it, so the app
sends the same message again. Each student here does exactly that: send a
message with an Idempotency-Key header, read the whole reply, then send the
same message with the same key again.

Then it counts, per student, how many times the message is in the
conversation and how many turns the student was charged for. Without
idempotency the server can't tell a resend from a new message: 2 and 2. With
it, the second request plays back the first answer: 1 and 1. Results are
appended to loadtest/results.jsonl.
"""

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import BOT, PASSWORD, RESULTS  # noqa: E402

QUESTION = "What time does the café open on Sunday?"


async def read_reply(client: httpx.AsyncClient, path: str, key: str) -> dict:
    events, replayed = [], None
    async with client.stream("POST", path, json={"text": QUESTION}, headers={"Idempotency-Key": key}) as r:
        replayed = r.headers.get("idempotent-replayed")
        if r.status_code != 200:
            return {"status": r.status_code, "events": [], "replayed": replayed}
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                events.append(line[6:].strip())
    return {"status": 200, "events": events, "replayed": replayed}


async def student(base: str, prefix: str, index: int) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(10, read=180)) as client:
        name = f"lt{index:03d}"
        (await client.post(f"{prefix}/auth/login", json={"username": name, "password": PASSWORD})).raise_for_status()
        before = (await client.get(f"{prefix}/auth/me")).json().get("usage", {}).get("turns")
        thread = (await client.post(f"{prefix}/bots/{BOT}/threads")).json()["thread_id"]
        key = str(uuid.uuid4())
        path = f"{prefix}/bots/{BOT}/threads/{thread}/messages"
        first = await read_reply(client, path, key)
        again = await read_reply(client, path, key)  # "the connection dropped": the app resends
        history = (await client.get(f"{prefix}/bots/{BOT}/threads/{thread}")).json()["messages"]
        after = (await client.get(f"{prefix}/auth/me")).json().get("usage", {}).get("turns")
        return {"times_in_conversation": sum(m["role"] == "user" and m["text"] == QUESTION for m in history),
                "turns_charged": after - before if isinstance(after, int) and isinstance(before, int) else None,
                "first_ok": "done" in first["events"], "first_status": first["status"],
                "first_events": first["events"][-2:], "resend_status": again["status"],
                "resend_answered": "done" in again["events"], "resend_replayed": again["replayed"] == "true"}


async def main(base: str, prefix: str, users: int, label: str):
    rows = await asyncio.gather(*(student(base, prefix, i) for i in range(users)))
    charged = [r["turns_charged"] for r in rows if r["turns_charged"] is not None]
    result = {
        "students": users,
        "message_in_conversation_twice": sum(r["times_in_conversation"] >= 2 for r in rows),
        "turns_run": sum(r["times_in_conversation"] for r in rows),
        "turns_charged": sum(charged) if charged else None,
        "resends_answered": sum(r["resend_answered"] for r in rows),
        "resends_played_back": sum(r["resend_replayed"] for r in rows),
        "first_sends_ok": sum(r["first_ok"] for r in rows),
        "first_send_problems": sorted({f"{r['first_status']} {r['first_events']}" for r in rows if not r["first_ok"]}),
    }
    print(json.dumps(result, indent=2))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "label": label, **result}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--prefix", default="", help="'/v1' once the API is versioned")
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--label", default="idempotency")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.prefix, args.users, args.label))
