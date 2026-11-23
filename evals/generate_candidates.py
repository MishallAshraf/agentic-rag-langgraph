"""Draft more test questions with an LLM, for a human to review.

    python evals/generate_candidates.py            # 10 drafts into evals/candidates.yaml
    python evals/generate_candidates.py --n 20 --seed 7

Writing test cases by hand is slow, so the judge model drafts them: for each
randomly chosen passage it writes a question a student might ask, in different
words from the passage (so the test checks meaning-based retrieval, not word
matching), plus the facts a correct answer contains.

Every draft is saved with `approved: false`. The runner ignores drafts until a
human has read the passage's page, fixed or deleted the draft, and set
`approved: true`. An LLM writing the test AND grading the answer, with no
human in between, would measure nothing but its own opinions.
"""

import argparse
import random

import yaml
from common import CANDIDATES_FILE, get_judge_llm, load_cases
from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

PROMPT = """You write test questions for a study assistant that answers from course material on counselling and psychotherapy.
Given one passage, write a question a student might really ask that the passage answers.
- Paraphrase: avoid the passage's distinctive words and names, so finding it needs understanding, not word matching.
- The question must be answerable from this passage alone, with a clear answer.
- If the passage has no clear, self-contained fact worth testing (a fragment, a list of references, a transition), set usable to false."""


class Draft(BaseModel):
    usable: bool = Field(description="False if the passage has nothing worth testing.")
    question: str = Field(description="The student's question.")
    expected_behaviour: str = Field(description="One sentence: what a correct reply does.")
    key_points: list[str] = Field(description="2-4 short facts from the passage a correct reply contains.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    from retrieval import get_retriever

    existing = yaml.safe_load(CANDIDATES_FILE.read_text(encoding="utf-8")) if CANDIDATES_FILE.exists() else []
    existing = existing or []
    used_ids = {c["id"] for c in existing} | {c["id"] for c in load_cases(include_candidates=False)}

    # Long passages only: short chunks are usually headings or fragments.
    passages = [d for d in get_retriever().docs if len(d.page_content) > 600]
    random.Random(args.seed).shuffle(passages)
    writer = get_judge_llm().with_structured_output(Draft, method="json_schema")

    drafts = []
    for doc in passages:
        if len(drafts) >= args.n:
            break
        page = doc.metadata.get("page", 0) + 1
        draft_id = f"gen-p{page}-{doc.metadata.get('start_index', 0)}"
        if draft_id in used_ids:
            continue
        draft = writer.invoke([SystemMessage(PROMPT), HumanMessage(" ".join(doc.page_content.split()))])
        if not draft.usable:
            continue
        print(f"  page {page}: {draft.question}")
        drafts.append({
            "id": draft_id, "category": "paraphrased", "generated": True, "approved": False,
            "turns": [draft.question], "expected_behaviour": draft.expected_behaviour,
            "key_points": draft.key_points, "expected_pages": [page],
        })

    header = ("# LLM-drafted test cases. Check each against its page in the PDF, fix or delete it,\n"
              "# then set approved: true. Unapproved drafts are ignored by run_evals.py.\n")
    CANDIDATES_FILE.write_text(header + yaml.safe_dump(existing + drafts, sort_keys=False, allow_unicode=True,
                                                       width=110), encoding="utf-8")
    print(f"\nAdded {len(drafts)} drafts to {CANDIDATES_FILE.name} (approved: false). Review them before use.")


if __name__ == "__main__":
    main()
