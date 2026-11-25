"""What do students see when an API copy dies under load? (Lampus Phase 1, Step 9)

    python loadtest/bench_failover.py --url http://127.0.0.1:8001   # one copy, no load balancer
    python loadtest/bench_failover.py --url http://127.0.0.1:8000   # nginx in front of 3 copies

Students sign in and chat without pause for --seconds. After --kill-after
seconds, API copy 1 is killed (SIGKILL: a crash, not a polite shutdown). Every
turn is recorded with when it started and ended and whether a complete answer
came back. A student whose turn fails waits a second and tries again, like a
person pressing "send" again.

Counting failed turns would mislead: a student refused at once retries every
second and piles up failures. So the numbers are per student:

  - replies cut off: turns that were streaming when the copy died;
  - students left without an answer until the end of the test;
  - the longest time from the crash until a student got a complete answer again.

Results are appended to loadtest/results.jsonl.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import BOT, PASSWORD, QUESTIONS, RESULTS, one_turn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


async def student(base: str, index: int, start: float, deadline: float, out: list):
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(10, read=120)) as client:
        thread, t = None, 0
        while time.monotonic() < deadline:
            began = time.monotonic() - start
            try:
                if thread is None:  # sign in and open a conversation (again, after a failure here)
                    r = await client.post("/auth/login", json={"username": f"lt{index:03d}", "password": PASSWORD})
                    r.raise_for_status()
                    r = await client.post(f"/bots/{BOT}/threads")
                    r.raise_for_status()
                    thread = r.json()["thread_id"]
                result = await one_turn(client, thread, QUESTIONS[(index + t) % len(QUESTIONS)])
            except httpx.HTTPError as e:
                result = {"ok": False, "error": type(e).__name__}
            out.append({"student": index, "at": began, "end": time.monotonic() - start, **result})
            t += 1
            if not result["ok"]:
                await asyncio.sleep(1)


async def killer(start: float, after: float) -> float | None:
    await asyncio.sleep(after)
    try:
        os.kill(int((ROOT / "data" / "loadtest" / "api_1.pid").read_text()), signal.SIGKILL)
        return time.monotonic() - start
    except (OSError, ValueError):
        return None


async def main(base: str, users: int, seconds: float, kill_after: float, label: str):
    out: list[dict] = []
    start = time.monotonic()
    deadline = start + seconds
    *_, killed_at = await asyncio.gather(*(student(base, i, start, deadline, out) for i in range(users)),
                                         killer(start, kill_after))
    kill = killed_at or kill_after
    cut = [r for r in out if r["at"] < kill <= r["end"] and not r["ok"]]
    after = [r for r in out if r["at"] >= kill]
    errors: dict[str, int] = {}
    for r in cut + after:
        if not r["ok"]:
            errors[r["error"]] = errors.get(r["error"], 0) + 1
    # For each student: seconds from the crash until the end of their first complete answer after it.
    waits = []
    for i in range(users):
        answered = [r["end"] for r in out if r["student"] == i and r["ok"] and r["end"] > kill]
        waits.append(min(answered) - kill if answered else None)
    recovered = [w for w in waits if w is not None]
    result = {
        "users": users, "seconds": seconds, "killed_at_s": round(kill, 1),
        "replies_cut_off": len(cut),
        "students_answered_again": len(recovered),
        "students_never_answered_again": users - len(recovered),
        "longest_wait_for_an_answer_s": round(max(recovered), 1) if recovered else None,
        "turns_ok_after_kill": sum(r["ok"] for r in after), "turns_failed_after_kill": sum(not r["ok"] for r in after),
        "errors": errors,
    }
    print(json.dumps(result, indent=2))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "label": label, **result}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--kill-after", type=float, default=15)
    parser.add_argument("--label", default="failover")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.users, args.seconds, args.kill_after, args.label))
