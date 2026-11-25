"""Do bot builds survive several API copies and a crash? (Lampus Phase 1, Step 8)

    python loadtest/bench_builds.py              # copies on 8001 and 8002 (loadtest/start_copies.sh 2)
    python loadtest/bench_builds.py --kill worker   # needs WORKERS=2: kill the worker instead

An admin uploads the café bot's two documents to copy A and starts a build.
Then, from the outside:

  1. Can copy B see the build? (An admin's next page load may reach any copy.)
  2. Two seconds into the build, copy A (or, with --kill worker, the worker
     running the build) is killed. Does the build still finish, as seen from
     copy B?

Builds that live in the memory of the copy that started them fail both. With
the queue, killing copy A doesn't touch the build at all; killing the worker
leaves its job unacknowledged, and another worker takes it over once it has
been idle for QUEUE_CLAIM_IDLE_MS. The
fake model (mock_llm.py) answers the drafting calls, so the build may end in
"review" or "failed" (its drafts don't pass every check); either counts as
finished: what matters is that the work was not lost. Results are appended
to loadtest/results.jsonl.
"""

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RESULTS = Path(__file__).parent / "results.jsonl"
DOCS = ROOT / "documents" / "uploads" / "customer_handbook"
ADMIN, PASSWORD = "ltadmin", "loadtest-admin-password-not-secret"
FINISHED = {"review", "failed", "saved", "rejected"}


async def make_admin():
    import engine.db  # noqa: F401  (Windows event loop)
    from engine.auth import AuthStore

    store = await AuthStore().open()
    try:
        await store.create_user(ADMIN, PASSWORD, "admin")
    except Exception:  # noqa: BLE001 - already there from an earlier run
        pass
    await store.close()


def kill(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGKILL)  # a crash, not a polite shutdown
        return True
    except OSError:
        return False


async def running_worker_pid(client: httpx.AsyncClient, build_id: str) -> int | None:
    """The process id of the worker running the build ("host:pid"), once one has taken it."""
    for _ in range(100):
        build = (await client.get(f"/admin/builds/{build_id}")).json()
        if build.get("status") == "running" and build.get("worker"):
            return int(build["worker"].split(":")[1])
        await asyncio.sleep(0.1)
    return None


async def main(copies: list[str], label: str, wait_s: float, target: str):
    await make_admin()
    a = httpx.AsyncClient(base_url=copies[0], timeout=30)
    b = httpx.AsyncClient(base_url=copies[1], timeout=30)
    for c in (a, b):
        (await c.post("/auth/login", json={"username": ADMIN, "password": PASSWORD})).raise_for_status()
    bot_id = f"lt_build_{secrets.token_hex(3)}"
    files = [("files", (p.name, p.read_bytes(), "text/markdown")) for p in sorted(DOCS.glob("*.md"))]
    r = await a.post("/admin/builds", data={"bot_id": bot_id, "use_case": "Answer customer questions about the "
                                            "cafe's menu, opening hours and policies."}, files=files)
    r.raise_for_status()
    build_id = r.json()["id"]
    seen_on_b = (await b.get(f"/admin/builds/{build_id}")).status_code == 200

    if target == "worker":
        pid = await running_worker_pid(b, build_id)
    else:
        pid = int((ROOT / "data" / "loadtest" / "api_1.pid").read_text())
    await asyncio.sleep(2)
    killed = pid is not None and kill(pid)
    start, status = time.monotonic(), None
    while time.monotonic() - start < wait_s:
        r = await b.get(f"/admin/builds/{build_id}")
        status = r.json()["status"] if r.status_code == 200 else f"HTTP {r.status_code}"
        if status in FINISHED:
            break
        await asyncio.sleep(1)
    result = {"build_visible_on_other_copy": seen_on_b, "killed": "worker" if target == "worker" else "copy A",
              "kill_ok": killed, "finished_after_crash": status in FINISHED, "last_status_seen_on_b": status,
              "seconds_after_kill": round(time.monotonic() - start, 1)}
    if r.status_code == 200:
        result["attempts"] = r.json().get("attempts")
    print(json.dumps(result))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "label": label, **result}) + "\n")
    await a.aclose()
    await b.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copies", default="http://127.0.0.1:8001,http://127.0.0.1:8002")
    parser.add_argument("--label", default="builds")
    parser.add_argument("--wait", type=float, default=180)
    parser.add_argument("--kill", choices=["api", "worker"], default="api")
    args = parser.parse_args()
    asyncio.run(main(args.copies.split(","), args.label, args.wait, args.kill))
