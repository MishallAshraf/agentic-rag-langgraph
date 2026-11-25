"""Load test: many students chatting at once, measured from the outside.

    python loadtest/run.py setup --users 50          # make throwaway users in the load-test database
    python loadtest/run.py run --users 20 --turns 3  # 20 students at once, 3 messages each

Each virtual student signs in, starts a conversation and sends messages one
after another, like a real person waiting for each reply. For every message we
record:
  - time to first token: from sending until the first word of the answer arrives
    (what the student feels as "it's thinking")
  - turn time: until the final, checked answer arrives
  - errors: anything other than a complete answer (HTTP errors, timeouts, an
    "error" event in the stream)

Run it against an API started with the load-test settings (see loadtest/README.md):
a separate user and checkpoint database, and LLM_BASE_URL pointing at the fake
model in mock_llm.py, so the numbers show the platform, not the model, and cost
nothing. Results are printed and appended to loadtest/results.jsonl.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(__file__).parent / "results.jsonl"
BOT = "customer_handbook"  # the fictional café bot: its documents are in the repository
PASSWORD = "loadtest-password-not-secret"  # throwaway users in a throwaway database
QUESTIONS = ["What vegan options do you have?", "Can I get a refund for a cold coffee?",
             "Do you take reservations for groups?", "Which drinks contain nuts?",
             "What time does the café open on Sunday?"]


# ------------------------------------------------------------------ setup

async def setup(users: int):
    sys.path.insert(0, str(ROOT))
    from auth import AuthStore  # reads AUTH_DB from the environment

    store = await AuthStore().open()
    made = 0
    for i in range(users):
        name = f"lt{i:03d}"
        try:
            await store.create_user(name, PASSWORD)
            made += 1
        except Exception:  # noqa: BLE001 - already exists from an earlier setup
            pass
        await store.grant(name, BOT)
    await store.close()
    print(f"{made} new users, {users} granted {BOT} (database: {os.environ.get('AUTH_DB')})")


# ------------------------------------------------------------------ one student

async def student(base: str, index: int, turns: int, timeout: float, out: list):
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(10, read=timeout)) as client:
        r = await client.post("/auth/login", json={"username": f"lt{index:03d}", "password": PASSWORD})
        if r.status_code != 200:
            out.append({"ok": False, "error": f"login {r.status_code}"})
            return
        r = await client.post(f"/bots/{BOT}/threads")
        if r.status_code != 201:
            out.append({"ok": False, "error": f"thread {r.status_code}"})
            return
        thread = r.json()["thread_id"]
        for t in range(turns):
            out.append(await one_turn(client, thread, QUESTIONS[(index + t) % len(QUESTIONS)]))


async def one_turn(client: httpx.AsyncClient, thread: str, question: str) -> dict:
    start = time.perf_counter()
    first_token, event_name = None, None
    try:
        async with client.stream("POST", f"/bots/{BOT}/threads/{thread}/messages",
                                 json={"text": question}) as r:
            if r.status_code != 200:
                return {"ok": False, "error": f"http {r.status_code}"}
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    if event_name == "token" and first_token is None:
                        first_token = time.perf_counter() - start
                    elif event_name == "error":
                        return {"ok": False, "error": "error event"}
                    elif event_name == "done":
                        return {"ok": True, "ttft": first_token, "turn": time.perf_counter() - start}
        return {"ok": False, "error": f"stream ended without an answer (last event: {event_name})"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": type(e).__name__}


# ------------------------------------------------------------------ the run

def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


async def run(base: str, users: int, turns: int, timeout: float, label: str):
    out: list[dict] = []
    start = time.perf_counter()
    await asyncio.gather(*(student(base, i, turns, timeout, out) for i in range(users)))
    wall = time.perf_counter() - start

    ok = [r for r in out if r["ok"]]
    ttfts = [r["ttft"] for r in ok if r["ttft"] is not None]
    turn_times = [r["turn"] for r in ok]
    errors: dict[str, int] = {}
    for r in out:
        if not r["ok"]:
            errors[r["error"]] = errors.get(r["error"], 0) + 1
    summary = {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": label,
        "users": users, "turns_each": turns, "attempted": users * turns, "completed": len(ok),
        "errors": errors, "wall_s": round(wall, 1),
        "throughput_turns_per_s": round(len(ok) / wall, 2),
        "ttft_p50_s": round(pct(ttfts, 50), 2) if ttfts else None,
        "ttft_p95_s": round(pct(ttfts, 95), 2) if ttfts else None,
        "turn_p50_s": round(pct(turn_times, 50), 2) if turn_times else None,
        "turn_p95_s": round(pct(turn_times, 95), 2) if turn_times else None,
        "turn_mean_s": round(statistics.mean(turn_times), 2) if turn_times else None,
    }
    print(json.dumps(summary, indent=2))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--users", type=int, default=50)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8000")
    r.add_argument("--users", type=int, default=10)
    r.add_argument("--turns", type=int, default=3)
    r.add_argument("--timeout", type=float, default=180)
    r.add_argument("--label", default="", help="what this run measures, e.g. 'baseline, 1 API process'")
    args = parser.parse_args()
    if args.command == "setup":
        asyncio.run(setup(args.users))
    else:
        asyncio.run(run(args.url, args.users, args.turns, args.timeout, args.label))


if __name__ == "__main__":
    main()
