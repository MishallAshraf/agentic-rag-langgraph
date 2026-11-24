"""Record real conversations for the frontend's replay demo.

Runs against the local API (python api.py) and saves every streamed event with
its timing to src/demo/recordings.json. The replay build (VITE_REPLAY=1) plays
these back, so the public demo needs no server, no key and costs nothing.

    DEMO_USER=... DEMO_PASSWORD_FILE=path python frontend/scripts/record_demo.py [bot_id ...]

Only bots whose documents can be shown publicly are recorded. A research answer
that cites anything outside docs/adr is dropped.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

API = os.getenv("API_URL", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parent.parent / "src" / "demo" / "recordings.json"

QUESTIONS = {
    "architecture_guide": None,  # None: the bot's own suggested questions
    "research_assistant": [
        "How does the project check that answers are faithful to the sources, and how does it guard against "
        "prompt injection?",
        "How was retrieval quality measured, and why did that lead to adding a reranker?",
    ],
}


def read_events(response):
    """Yield (seconds since start, event dict) from an SSE response."""
    start = time.monotonic()
    block: list[str] = []
    for line in response.iter_lines():
        if line:
            block.append(line)
            continue
        kind, data = "message", []
        for part in block:
            if part.startswith("event:"):
                kind = part[6:].strip()
            elif part.startswith("data:"):
                data.append(part[5:].removeprefix(" "))
        block = []
        if data:
            yield round(time.monotonic() - start, 3), {"type": kind, "data": json.loads("\n".join(data))}


def public(events) -> bool:
    done = next((e for _, e in events if e["type"] == "done"), None)
    if not done or not done["data"]["answer"] or done["data"]["guard"] == "error":
        print("  dropped: the turn failed")
        return False
    for source in done["data"]["sources"]:
        if ".md" not in source or ".pdf" in source.lower():
            print(f"  dropped: cites {source!r}")
            return False
    return True


def main():
    password = Path(os.environ["DEMO_PASSWORD_FILE"]).read_text().strip()
    with httpx.Client(base_url=API, timeout=300) as client:
        client.post("/auth/login", json={"username": os.environ["DEMO_USER"], "password": password}).raise_for_status()
        bots = {b["id"]: b for b in client.get("/bots").json()}
        # Re-running adds to the saved recordings, so one bot can be recorded again on its own.
        recorded = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"bots": [], "turns": {}}
        recorded["recorded_at"] = time.strftime("%Y-%m-%d")
        only = set(sys.argv[1:])
        for bot_id, questions in QUESTIONS.items():
            if only and bot_id not in only:
                continue
            bot = bots[bot_id]
            questions = questions or bot["suggested_questions"]
            kept = []
            for q in questions:
                print(f"{bot_id}: {q}")
                thread = client.post(f"/bots/{bot_id}/threads").json()["thread_id"]
                with client.stream("POST", f"/bots/{bot_id}/threads/{thread}/messages", json={"text": q},
                                   headers={"Accept": "text/event-stream"}) as r:
                    r.raise_for_status()
                    events = list(read_events(r))
                if public(events):
                    recorded["turns"][f"{bot_id}|{q}"] = [{"t": t, **e} for t, e in events]
                    kept.append(q)
                    print(f"  kept: {len(events)} events, {events[-1][0]:.1f}s")
            saved = {b["id"]: b for b in recorded["bots"]}
            old = saved.get(bot_id, {}).get("suggested_questions", [])
            kept = old + [q for q in kept if q not in old]
            if kept:
                saved[bot_id] = {**bot, "suggested_questions": kept}
            recorded["bots"] = [saved[b] for b in QUESTIONS if b in saved]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(recorded, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Saved {len(recorded['turns'])} turns to {OUT}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
