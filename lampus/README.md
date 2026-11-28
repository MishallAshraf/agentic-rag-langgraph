# Lampus

Lampus is a bilingual (English and Urdu / Roman Urdu) AI tutoring product for schools and private tutors, built on the bot-builder engine in [`../engine`](../engine). *Pass the light.*

The engine stays a general bot builder: agents, retrieval, guardrails, sign-in, quotas, evaluation, the API and the frontend. This folder holds what is specific to tutoring, which the builder can't make on its own:

- tutor bots per subject, grounded in a textbook with page citations
- a check that the tutor gives hints rather than answers
- homework grading, mastery tracking and the next-exercise recommender
- teacher dashboards and parent reports
- Urdu and Roman Urdu support
- each school as its own workspace

Phase 1 made the engine production-ready (Linux, Postgres, Redis, a queue, several API copies). Phase 2 builds the tutor MVP, one step at a time.

## What's here

| File | What it does |
| --- | --- |
| `textbooks/fetch.py` | Downloads the textbook: the PDF and the web pages of chapters 4–6 ([textbooks/README.md](textbooks/README.md): source and license) |
| `textbook.py` | Loads the textbook: text and math from the web pages, printed page numbers from the PDF, chunks cut by the book's structure (Step 11) |
| `ingest_textbook.py` | Puts the chunks in the vector store (`prealgebra` collection); `--fixed` builds the engine-loader version to compare against |
| `evals/questions.jsonl` | 30 student questions, each with a phrase from the book that fixes the page answering it |
| `evals/answer_seeking.jsonl` | 5 questions asking for exercise answers, to check the answer key never reaches the tutor |
| `bench/bench_retrieval.py` | How many of the 30 questions find their page in the top 5, for each loader and search mode |

```bash
python -m lampus.textbooks.fetch
python -m lampus.ingest_textbook --fixed && python -m lampus.ingest_textbook
python -m lampus.bench.bench_retrieval
```

## Results so far

| Step | Change | Measured |
| --- | --- | --- |
| 11 | Textbook from web text and math, printed pages from the PDF, book-structured chunks ([ADR 0033](../docs/adr/0033-textbook-ingestion-printed-pages-and-structure.md)) | Questions finding their page in the top 5: 0 → 24 of 30 (default rerank), 27 with vector search; answer key in results: 5 → 0 of 5 |

The product plan, build phases and how each course topic maps to a feature are in [ai-system-design-lab](https://github.com/MishallAshraf/ai-system-design-lab/blob/main/docs/lampus-product.md).
