"""Can a school's server tell Lampus's webhooks from fakes? (Lampus Phase 1, Step 10)

    WEBHOOK_URL=http://127.0.0.1:8950/hook WEBHOOK_SECRET=whsec_... WORKERS=1 bash loadtest/start_copies.sh 1
    python loadtest/bench_webhooks.py          # same WEBHOOK_URL and WEBHOOK_SECRET in its environment

A stand-in for a school's server listens on port 8950 and records every
request. It is "down" (503) for its first 3 seconds, so deliveries must retry.
Then:

  - Lampus sends 50 genuine events the real way: queued in Redis, signed and
    delivered by the worker (engine/webhooks.py);
  - an attacker who knows the URL but not the secret sends 50 forged messages,
    50 genuine ones with the body changed, 50 genuine ones sent again (replays)
    and 50 genuine-looking ones recorded 10 minutes ago.

The same recorded traffic is then judged by two receivers: one that accepts
every POST (no checks), and one that checks the signature and timestamp and
drops ids it has seen (engine.webhooks.verify plus a set of ids). Results are
appended to loadtest/results.jsonl.
"""

import asyncio
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import webhooks  # noqa: E402

RESULTS = Path(__file__).parent / "results.jsonl"
PORT, GENUINE, ATTACKS, DOWN_S = 8950, 50, 50, 3

received: list[dict] = []
started = time.monotonic()
app = FastAPI()


@app.post("/hook")
async def hook(request: Request):
    body = await request.body()
    received.append({"headers": {k.lower(): v for k, v in request.headers.items()}, "body": body,
                     "kind": request.headers.get("x-bench-kind", "genuine")})
    return Response(status_code=503 if time.monotonic() - started < DOWN_S else 200)


def judge(verifying: bool, secret: str) -> dict:
    """How many requests of each kind a receiver would act on."""
    seen: set[str] = set()
    accepted: dict[str, int] = {}
    for r in received:
        if verifying:
            if not webhooks.verify(secret, r["headers"], r["body"]):
                continue
            if r["headers"]["webhook-id"] in seen:  # a retry or a replay: already handled
                continue
            seen.add(r["headers"]["webhook-id"])
        accepted[r["kind"]] = accepted.get(r["kind"], 0) + 1
    return accepted


async def attack(url: str, secret: str):
    genuine = [r for r in received if r["kind"] == "genuine"][:ATTACKS]
    async with httpx.AsyncClient() as client:
        for i in range(ATTACKS):
            body = json.dumps({"type": "conversation.flagged", "data": {"thread_id": secrets.token_hex(16)}}).encode()
            fake = {"webhook-id": f"msg_{secrets.token_hex(16)}", "webhook-timestamp": str(int(time.time())),
                    "webhook-signature": "v1," + secrets.token_urlsafe(32), "x-bench-kind": "forged"}
            await client.post(url, content=body, headers=fake)
            g = genuine[i % len(genuine)]
            keep = {k: v for k, v in g["headers"].items() if k.startswith("webhook-")}
            await client.post(url, content=g["body"].replace(b'"data": {', b'"data": {"extra": 1, '),
                              headers={**keep, "x-bench-kind": "tampered"})
            await client.post(url, content=g["body"], headers={**keep, "x-bench-kind": "replayed"})
            # A message recorded 10 minutes ago (a genuine signature, but old).
            old = int(time.time()) - 600
            stale = {"webhook-id": f"msg_{secrets.token_hex(16)}", "webhook-timestamp": str(old)}
            stale["webhook-signature"] = webhooks.sign(secret, stale["webhook-id"], old, g["body"])
            await client.post(url, content=g["body"], headers={**stale, "x-bench-kind": "stale"})


async def main(label: str):
    secret = os.environ["WEBHOOK_SECRET"]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)
    for i in range(GENUINE):
        await webhooks.emit("build.finished", {"build_id": f"bench_{i}", "status": "review"})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:  # wait for the worker to deliver (and retry) all of them
        ok = {r["headers"].get("webhook-id") for r in received
              if r["kind"] == "genuine" and webhooks.verify(secret, r["headers"], r["body"])}
        if len(ok) >= GENUINE:
            break
        await asyncio.sleep(0.5)
    await attack(f"http://127.0.0.1:{PORT}/hook", secret)
    attempts = [r for r in received if r["kind"] == "genuine"]
    naive, checked = judge(False, secret), judge(True, secret)
    attack_kinds = ("forged", "tampered", "replayed", "stale")
    result = {
        "genuine_events": GENUINE,
        "delivery_attempts": len(attempts),
        "genuine_delivered": checked.get("genuine", 0),
        "attacks_sent": ATTACKS * len(attack_kinds),
        "attacks_accepted_no_checks": sum(naive.get(k, 0) for k in attack_kinds),
        "attacks_accepted_verified": sum(checked.get(k, 0) for k in attack_kinds),
        "accepted_verified_by_kind": {k: checked.get(k, 0) for k in attack_kinds},
    }
    print(json.dumps(result, indent=2))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "label": label, **result}) + "\n")
    server.should_exit = True
    await serving


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "webhooks"))
