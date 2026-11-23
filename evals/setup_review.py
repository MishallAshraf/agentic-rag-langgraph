"""Put a run's answers in a Langfuse annotation queue, for a human to grade.

    python evals/setup_review.py                  # the latest run in evals/results/
    python evals/setup_review.py --run baseline-20261123-215403

Creates (once) a boolean score called human_correct and a queue that asks for
it, then adds every trace of the run to the queue. In Langfuse, open
Annotation Queues -> study-assistant-review and grade each answer: is it
correct for a student of this course? Then run calibrate.py to see how often
the LLM judge agrees with you.
"""

import argparse
import json

from common import RESULTS_DIR, get_langfuse, langfuse_enabled

SCORE_NAME = "human_correct"
QUEUE_NAME = "study-assistant-review"


def find_or_create_score_config(lf):
    for config in lf.api.score_configs.get(limit=100).data:
        if config.name == SCORE_NAME and not config.is_archived:
            return config
    return lf.api.score_configs.create(
        name=SCORE_NAME, data_type="BOOLEAN",
        description="A human's verdict: is this reply correct and appropriate for a student of the course?")


def find_or_create_queue(lf, config_id):
    for queue in lf.api.annotation_queues.list_queues(limit=100).data:
        if queue.name == QUEUE_NAME:
            return queue
    return lf.api.annotation_queues.create_queue(
        name=QUEUE_NAME, score_config_ids=[config_id],
        description="Grade each study-assistant reply. Used to check how far the LLM judge can be trusted.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", help="results file name without .json (default: the latest)")
    args = parser.parse_args()
    if not langfuse_enabled():
        raise SystemExit("Needs Langfuse keys in .env.")

    runs = sorted(RESULTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    path = RESULTS_DIR / f"{args.run}.json" if args.run else (runs[-1] if runs else None)
    if not path or not path.exists():
        raise SystemExit("No run found. Run evals/run_evals.py first.")
    run = json.loads(path.read_text(encoding="utf-8"))

    lf = get_langfuse()
    config = find_or_create_score_config(lf)
    queue = find_or_create_queue(lf, config.id)
    queued = {i.object_id for i in lf.api.annotation_queues.list_queue_items(queue.id, limit=100).data}
    added = 0
    for case in run["cases"]:
        if case["trace_id"] and case["trace_id"] not in queued:
            lf.api.annotation_queues.create_queue_item(queue.id, object_id=case["trace_id"], object_type="TRACE")
            added += 1
    print(f"Added {added} traces from {path.stem} to the queue '{QUEUE_NAME}'.")
    print("Grade them in Langfuse: Annotation Queues -> " + QUEUE_NAME + ", then run evals/calibrate.py")


if __name__ == "__main__":
    main()
