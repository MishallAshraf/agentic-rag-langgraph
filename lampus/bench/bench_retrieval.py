"""Does search find the right textbook page? Lampus Phase 2, Step 11 (topics 18, 41).

    python -m lampus.ingest_textbook --fixed && python -m lampus.ingest_textbook
    python -m lampus.bench.bench_retrieval

For each of 30 student questions (lampus/evals/questions.jsonl), the page that
answers it is found independently of either loader: the question carries a
phrase from the book, and the expected pages are the printed pages of the PDF
where that phrase appears (chapters 4-6). A question counts as found when one
of the top 5 results cites one of those pages.

Compared, for each search mode (vector, keyword, hybrid, rerank):

  before          the engine's PDF loader, 1,000-character chunks, citing the PDF's page
  before, fixed   the same chunks, but with the page number corrected to the printed one
                  (separates "wrong page number" from "wrong chunk")
  after           web text with math, cut by the book's structure, printed pages

Also: how often the top 5 for 5 answer-seeking questions include answer-key text.
Writes lampus/bench/results/retrieval.json.
"""

import json
import statistics
import time
from pathlib import Path

from engine.retrieval import get_retriever
from lampus.ingest_textbook import CHAPTERS, COLLECTION, FIXED_COLLECTION, fixed_ranges, pdf_pages
from lampus.textbook import words

HERE = Path(__file__).parent
EVALS = HERE.parent / "evals"
MODES = ("vector", "keyword", "hybrid", "rerank")
K = 5


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (EVALS / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def expected_pages(phrase: str) -> set[int]:
    """Printed pages, in chapters 4-6, whose text contains the phrase's words in order."""
    pages = pdf_pages()
    run = " ".join(words(phrase))
    found = set()
    for chapter in CHAPTERS:
        lo, hi = pages.range_of(f"Chapter {chapter} ", level=1)
        found |= {pages.printed[p] for p in range(lo, hi) if run in pages.text[p]}
    return found


def cited(doc, corrected: bool = False) -> tuple[int, int]:
    """The printed pages a result points the student to."""
    m = doc.metadata
    if "page_end" in m:  # the textbook loader: printed pages already
        return m["page"], m["page_end"]
    if corrected:
        p = pdf_pages().printed[m["page"]]
        return p, p
    return m["page"] + 1, m["page"] + 1  # what the engine shows: the PDF's page number


def is_answer_key(doc) -> bool:
    m = doc.metadata
    if m.get("section") == "answers":
        return True
    answer_ranges = fixed_ranges()[len(CHAPTERS):]
    return "page_end" not in m and any(lo <= m["page"] < hi for lo, hi in answer_ranges)


def main():
    questions = load("questions.jsonl")
    seeking = load("answer_seeking.jsonl")
    for q in questions:
        q["pages"] = sorted(expected_pages(q["phrase"]))
        if not q["pages"]:
            raise SystemExit(f"{q['id']}: phrase not found in the PDF: {q['phrase']!r}")

    results = {"k": K, "questions": len(questions), "expected_pages": {q["id"]: q["pages"] for q in questions},
               "setups": {}}
    for label, collection, corrected in (("before", FIXED_COLLECTION, False),
                                         ("before, page corrected", FIXED_COLLECTION, True),
                                         ("after", COLLECTION, False)):
        retriever = get_retriever(collection)
        rows = {}
        for mode in MODES:
            found, times, misses = 0, [], []
            for q in questions:
                started = time.perf_counter()
                docs = retriever.search(q["question"], k=K, mode=mode)
                times.append((time.perf_counter() - started) * 1000)
                if any(lo <= p <= hi for d in docs for lo, hi in [cited(d, corrected)] for p in q["pages"]):
                    found += 1
                else:
                    misses.append(q["id"])
            leaks = sum(any(is_answer_key(d) for d in retriever.search(s["question"], k=K, mode=mode))
                        for s in seeking)
            rows[mode] = {"found": found, "leaky_questions": leaks, "ms_p50": round(statistics.median(times), 1),
                          "misses": misses}
            print(f"{label:24} {mode:8} found {found:>2}/{len(questions)}  "
                  f"answer key in top {K}: {leaks}/{len(seeking)}  p50 {rows[mode]['ms_p50']} ms")
        results["setups"][label] = {"collection": collection, "chunks": retriever.store.count(), "modes": rows}

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "retrieval.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
