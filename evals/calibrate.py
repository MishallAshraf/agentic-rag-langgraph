"""How far can the LLM judge be trusted? Compare it with a human's grades.

    python evals/calibrate.py                           # judge vs your grades
    python evals/calibrate.py --reference opus_correct  # judge vs another grader
    python evals/calibrate.py --reference opus_faithful --judge judge_faithful

For every trace a human graded in the annotation queue (human_correct), fetch
the judge's verdict on the same trace (judge_correct) and report:

  agreement   share of traces where both said the same
  kappa       Cohen's kappa: agreement corrected for chance. If 90% of answers
              are correct, a judge that always says "correct" agrees 90% of
              the time while knowing nothing; kappa would be 0 for it.
              Rough reading: < 0.4 poor, 0.4-0.6 moderate, 0.6-0.8 good, > 0.8 very good.
  disagreements, listed, because they show what the judge gets wrong -
              which is what to fix in the judge prompt.
"""

import argparse

from common import get_langfuse, langfuse_enabled
from setup_review import QUEUE_NAME, SCORE_NAME


def all_scores(lf, **filters):
    scores, cursor = [], None
    while True:
        # The v3 API leaves out the trace a score belongs to unless asked: fields="subject".
        page = lf.api.scores.get_many(limit=100, cursor=cursor, fields="subject", **filters)
        scores += page.data
        cursor = page.meta.cursor if len(page.data) == 100 else None  # a short page is the last
        if not cursor:
            return scores


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    p_human = sum(a for a, _ in pairs) / n
    p_judge = sum(b for _, b in pairs) / n
    expected = p_human * p_judge + (1 - p_human) * (1 - p_judge)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def trace_of(score) -> str | None:
    return score.subject.id if score.subject and score.subject.kind == "trace" else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default=SCORE_NAME,
                        help="the score to compare the judge with (default: your grades, human_correct)")
    parser.add_argument("--judge", default="judge_correct", help="the judge score to check")
    args = parser.parse_args()
    if not langfuse_enabled():
        raise SystemExit("Needs Langfuse keys in .env.")
    lf = get_langfuse()
    filters = {"name": args.reference}
    if args.reference == SCORE_NAME:  # human grades: only those given in the review queue
        queue = next((q for q in lf.api.annotation_queues.list_queues(limit=100).data
                      if q.name == QUEUE_NAME), None)
        if queue is None:
            raise SystemExit("No review queue yet. Run evals/setup_review.py first.")
        filters["queue_id"] = queue.id

    human = {trace_of(s): bool(s.value) for s in all_scores(lf, **filters) if trace_of(s)}
    if not human:
        raise SystemExit(f"No {args.reference} scores yet. Grade some traces in the queue '{QUEUE_NAME}'.")

    pairs, disagreements = [], []
    for trace_id, human_says in human.items():
        judged = all_scores(lf, name=args.judge, trace_id=trace_id)
        if not judged:
            continue
        judge_says = bool(judged[0].value)
        pairs.append((human_says, judge_says))
        if human_says != judge_says:
            disagreements.append((trace_id, human_says, judge_says, judged[0].comment))

    if not pairs:
        raise SystemExit("None of the graded traces has a judge score.")
    agreement = sum(a == b for a, b in pairs) / len(pairs)
    print(f"Graded by both: {len(pairs)} traces")
    print(f"Agreement:      {agreement:.0%}")
    print(f"Cohen's kappa:  {cohen_kappa(pairs):.2f}")
    if len(pairs) < 15:
        print("(Fewer than 15 traces: treat these numbers as a rough first look.)")
    for trace_id, human_says, judge_says, comment in disagreements:
        print(f"\n- trace {trace_id}: {args.reference} said {'yes' if human_says else 'no'}, "
              f"{args.judge} said {'yes' if judge_says else 'no'}\n  judge: {comment}")


if __name__ == "__main__":
    main()
