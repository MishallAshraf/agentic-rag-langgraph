"""Does the API keep its promises when it runs as several copies? (Lampus Phase 1, Step 7)

    python loadtest/bench_shared_state.py --copies http://127.0.0.1:8001,http://127.0.0.1:8002

Run it against two (or more) API copies sharing one Postgres, each started with
the load-test settings and API_RATE_LIMIT=10 (see loadtest/README.md, Step 7).
Users lt000 and lt001 must exist (python loadtest/run.py setup --users 2).

Two checks, from the outside:

  rate limit    one student fires 30 messages in a few seconds, spread over the
                copies. The limit is 10 per minute, so at most 10 should start.
                With a limiter kept in each copy's memory, each copy allows 10.
  one turn at   another student sends two messages to the same conversation at
  a time        the same moment, one to each copy. Only one should run: two turns
                writing one conversation at once can corrupt it. With a lock kept in
                each copy's memory, both run.

Results are printed and appended to loadtest/results.jsonl.
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

RESULTS = Path(__file__).parent / "results.jsonl"
BOT = "customer_handbook"
PASSWORD = "loadtest-password-not-secret"
BURST = 30


async def signed_in(base: str, user: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(10, read=60))
    r = await client.post("/auth/login", json={"username": user, "password": PASSWORD})
    r.raise_for_status()
    return client


async def status_of_message(client: httpx.AsyncClient, thread: str, read_all: bool) -> int:
    """Send one message; return the HTTP status (and, if read_all, wait for the reply to finish)."""
    async with client.stream("POST", f"/bots/{BOT}/threads/{thread}/messages",
                             json={"text": "What vegan options do you have?"}) as r:
        if read_all and r.status_code == 200:
            async for _ in r.aiter_lines():
                pass
        return r.status_code


async def rate_limit_check(copies: list[str]) -> dict:
    clients = [await signed_in(base, "lt000") for base in copies]
    threads = [(await clients[i % len(clients)].post(f"/bots/{BOT}/threads")).json()["thread_id"]
               for i in range(BURST)]
    statuses = await asyncio.gather(*(status_of_message(clients[i % len(clients)], threads[i], read_all=False)
                                      for i in range(BURST)))
    for c in clients:
        await c.aclose()
    return {"sent": BURST, "started": statuses.count(200), "refused_429": statuses.count(429),
            "other": len(statuses) - statuses.count(200) - statuses.count(429)}


async def lock_check(copies: list[str]) -> dict:
    a, b = await signed_in(copies[0], "lt001"), await signed_in(copies[1], "lt001")
    thread = (await a.post(f"/bots/{BOT}/threads")).json()["thread_id"]
    statuses = await asyncio.gather(status_of_message(a, thread, True), status_of_message(b, thread, True))
    await a.aclose()
    await b.aclose()
    return {"sent_at_once": 2, "ran": statuses.count(200), "refused_409": statuses.count(409)}


async def main(copies: list[str], label: str):
    if len(copies) < 2:
        raise SystemExit("Give at least two API copies.")
    rate = await rate_limit_check(copies)
    print("rate limit:", json.dumps(rate))
    lock = await lock_check(copies)
    print("one turn at a time:", json.dumps(lock))
    summary = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": label,
               "copies": len(copies), "rate_limit_per_min": 10, "rate_limit": rate, "lock": lock}
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copies", default="http://127.0.0.1:8001,http://127.0.0.1:8002")
    parser.add_argument("--label", default="shared state")
    args = parser.parse_args()
    asyncio.run(main(args.copies.split(","), args.label))
