"""A terminal client for the Step 11 API: chat with any bot over Server-Sent Events.

    python api.py                                         # in one terminal
    python api_client.py --bot architecture_guide         # in another
    python api_client.py --bot study_assistant --thread <id>   # continue a conversation

It shows the events as they arrive and, after each answer, how long the user
waited: until the first sign of progress, the first token of the answer, and
the final answer. Those are the numbers a frontend's perceived speed depends on.
"""

import argparse
import json
import time

import httpx
from httpx_sse import connect_sse

DEFAULT_URL = "http://127.0.0.1:8000"


def stream(client: httpx.Client, url: str, body: dict) -> dict:
    """POST, print the events, return the final event's data (done or approval)."""
    start, first_event, first_token, final = time.perf_counter(), None, None, {}
    current_message = None
    with connect_sse(client, "POST", url, json=body) as source:
        if source.response.status_code != 200:
            source.response.read()
            print(f"  [{source.response.status_code}] {source.response.json().get('detail')}")
            return {}
        for sse in source.iter_sse():
            now = time.perf_counter() - start
            first_event = first_event or now
            data = json.loads(sse.data)
            if sse.event == "token":
                if data["message_id"] != current_message:  # a new draft (or a revision after a fact check)
                    current_message = data["message_id"]
                    print("\n  [draft] ", end="")
                first_token = first_token or now
                print(data["text"], end="", flush=True)
            elif sse.event == "status":
                print(f"\n  [{now:5.1f}s] {data}", end="")
            elif sse.event in ("done", "approval", "error"):
                final = {"event": sse.event, **data}
                if sse.event == "error":
                    print(f"\n  [error: {data['type']}]", end="")
    total = time.perf_counter() - start
    print()
    if final.get("event") == "done":
        print(f"\nAI: {final['answer']}")
        for s in final["sources"]:
            print(f"  source: {s}")
    tok = f"{first_token:.1f}s" if first_token else "none"
    print(f"  [timing: first event {first_event or 0:.1f}s, first token {tok}, done {total:.1f}s]")
    return final


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", default="study_assistant")
    parser.add_argument("--thread")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    with httpx.Client(base_url=args.url, timeout=httpx.Timeout(10, read=180)) as client:
        bot = client.get(f"/bots/{args.bot}").raise_for_status().json()
        thread = args.thread or client.post(f"/bots/{args.bot}/threads").raise_for_status().json()["thread_id"]
        print(f"{bot['name']}  (conversation {thread})\n{bot['welcome']}\n(blank line to quit)")
        base = f"/bots/{args.bot}/threads/{thread}"
        while True:
            text = input("\nYou: ").strip()
            if not text:
                break
            final = stream(client, f"{base}/messages", {"text": text})
            while final.get("event") == "approval":
                for call in final["tool_calls"]:
                    print(f"\n  APPROVAL NEEDED: {call['name']} {call['args']}")
                answer = input("  Approve? [y/N]: ").strip().lower()
                decision = "approve" if answer in ("y", "yes") else "reject"
                final = stream(client, f"{base}/approval", {"decision": decision})
        print(f"Resume with --thread {thread}")


if __name__ == "__main__":
    main()
