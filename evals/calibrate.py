"""How far can the LLM judge be trusted? Compare it with a human's grades.

    python evals/calibrate.py

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

from common import get_langfuse, langfuse_enabled
from setup_review import QUEUE_NAME, SCORE_NAME


def all_scores(lf, **filters):
    scores, cursor = [], None
    while True:
        page = lf.api.scores.get_many(limit=100, cursor=cursor, **filters)
        scores += page.data
        cursor = getattr(page.meta, "next_cursor", None) if getattr(page, "meta", None) else None
        if not cursor:
            return scores


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    p_human = sum(a for a, _ in pairs) / n
    p_judge = sum(b for _, b in pairs) / n
    expected = p_human * p_judge + (1 - p_human) * (1 - p_judge)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def main():
    if not langfuse_enabled():
        raise SystemExit("Needs Langfuse keys in .env.")
    lf = get_langfuse()
    queue = next((q for q in lf.api.annotation_queues.list_queues(limit=100).data if q.name == QUEUE_NAME), None)
    if queue is None:
        raise SystemExit("No review queue yet. Run evals/setup_review.py first.")

    human = {s.trace_id: bool(s.value) for s in all_scores(lf, name=SCORE_NAME, queue_id=queue.id)}
    if not human:
        raise SystemExit(f"No {SCORE_NAME} scores yet. Grade some traces in the queue '{QUEUE_NAME}'.")

    pairs, disagreements = [], []
    for trace_id, human_says in human.items():
        judged = all_scores(lf, name="judge_correct", trace_id=trace_id)
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
        print(f"\n- trace {trace_id}: you said {'correct' if human_says else 'incorrect'}, "
              f"the judge said {'correct' if judge_says else 'incorrect'}\n  judge: {comment}")


if __name__ == "__main__":
    main()
