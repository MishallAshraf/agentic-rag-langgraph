"""Step 6c - Measure retrieval methods against each other on fixed questions.

Each question has a phrase that only the right passage contains. A method
"hits" if one of its top 4 chunks contains that phrase. This is a tiny preview
of the golden dataset in Step 8: without it, "better retrieval" is a guess.

Metrics:
  hit@4  share of questions where the right chunk is in the top 4
  MRR    mean of 1/rank of the right chunk (1.0 = always first, 0 = never found)

    python step6_compare.py
"""

import re
import time

from retrieval import get_retriever
from vectorstore import get_vectorstore

K = 4

# (question, phrase the right chunk must contain). Mix of exact-term questions
# (favour keywords) and paraphrases (favour meaning).
QUESTIONS = [
    ("Why is active listening important?", "importance of active listening"),
    ("What were Lambert's four common factors?", "hope and expectancy"),
    ("What are Frank's essential common factors of effective psychotherapy?", "healing setting"),
    ("What is the Dodo bird effect?", "dodo"),
    ("Why do different therapies often produce similar outcomes?", "dodo"),
    # The source has a typo, "eight-nine", for eighty-nine.
    ("How many factors did Grencavage and Norcross find?", "eight-nine common factors"),
    ("Which client characteristics affect how well therapy works?", "psychological mindedness"),
    ("Which professions provide counselling?", "occupational therapists"),
    ("What is the difference between a scientist-practitioner and a reflective-practitioner?", "reflective-practitioner"),
    ("What dimensions does listening involve?", "multidimensional process"),
    ("Is counselling an art or a science?", "art and science"),
    ("Does hope predict progress in therapy?", "good predictor of likely progress"),
]


def normalise(text: str) -> str:
    # Remove all spaces so a stray space inside a word ("c ounselling") does not
    # hide a correct hit in the old index.
    return re.sub(r"\s+", "", text.lower())


def rank_of(docs, phrase: str):
    target = normalise(phrase)
    return next((i for i, d in enumerate(docs, start=1) if target in normalise(d.page_content)), None)


def main():
    retriever = get_retriever()
    old_store = get_vectorstore()  # the Step 1 collection: pypdf, footers, references
    methods = {
        "step 1 vector": lambda q: old_store.similarity_search(q, k=K),
        "vector": lambda q: retriever.search(q, K, "vector"),
        "keyword": lambda q: retriever.search(q, K, "keyword"),
        "hybrid": lambda q: retriever.search(q, K, "hybrid"),
        "rerank": lambda q: retriever.search(q, K, "rerank"),
    }
    retriever.search("warm-up", K, "rerank")  # load the reranker before timing

    ranks = {name: [] for name in methods}
    times = {name: 0.0 for name in methods}
    for question, phrase in QUESTIONS:
        for name, run in methods.items():
            start = time.perf_counter()
            docs = run(question)
            times[name] += time.perf_counter() - start
            ranks[name].append(rank_of(docs, phrase))

    print("Rank of the right chunk per question (- = not in top 4)\n")
    print(f"{'question':58}" + "".join(f"{n:>15}" for n in methods))
    for i, (question, _) in enumerate(QUESTIONS):
        cells = "".join(f"{(ranks[n][i] or '-'):>15}" for n in methods)
        print(f"{question[:56]:58}{cells}")

    print()
    n = len(QUESTIONS)
    print(f"{'':58}" + "".join(f"{name:>15}" for name in methods))
    print(f"{'hit@4':58}" + "".join(f"{sum(r is not None for r in ranks[m]) / n:>15.2f}" for m in methods))
    print(f"{'MRR':58}" + "".join(f"{sum(1 / r for r in ranks[m] if r) / n:>15.2f}" for m in methods))
    print(f"{'average latency (ms)':58}" + "".join(f"{times[m] / n * 1000:>15.0f}" for m in methods))


if __name__ == "__main__":
    main()
