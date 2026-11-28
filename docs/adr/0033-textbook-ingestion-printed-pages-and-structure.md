# 0033. The tutor's textbook: text and math from the web pages, printed pages from the PDF, chunks cut by the book's structure

- Status: Accepted
- Date: 2026-11-27
- Step: Lampus Phase 2, Step 11

## Context

The tutor MVP answers from one textbook and cites its pages: OpenStax *Prealgebra 2e*, chapters 4–6 (Fractions, Decimals, Percents). Ingesting it with the engine as it was (Step 6's PDF loader, 1,000-character chunks with 150 overlap) has three problems.

1. **Wrong page numbers (topic 18).** The engine cites the PDF's page number. The book's printed numbering starts at chapter 1, so PDF page 285 is printed page 275. Every citation sends a student 10 pages off.
2. **The math is missing (topic 18).** The PDF draws every formula as vector shapes, not text. Example 4.19, "Simplify 10/15", extracts as "Simplify: ." The answer key is mostly numbers, so it comes out nearly empty. A math tutor reading this would see the words around every formula, never the formula itself.
3. **Chunks ignore the book (topic 41).**
   - Fixed-size chunks cut worked examples in half and mix the end of one topic with the start of the next.
   - They carry no idea of what they are, so the answer key is searchable like any other page. Asked "what is the answer to Try It 4.37?", search returned answer-key pages for 5 of 5 such questions.

## Decision

**Two copies of the same book, each used for what it has right** (`lampus/textbook.py`):

- **Text and math from the book's web pages.**
  - Formulas there are MathML, written out as a student would type them: `10/15`, `1 5/6`, `x^2`, `√(49)`, `(x + 1)/2`.
  - The pages also mark worked examples, "Try It" boxes, "How To" steps and exercises.
- **Printed page numbers from the PDF.**
  - The footers carry them ("4.2 • Multiply and Divide Fractions     275").
  - Each passage is found in the PDF by a run of six of its words, using words only, since the PDF lost the math.
  - The search moves forward through the section's pages, as the book reads.
  - 1,080 of 1,129 chunks (96%) were found this way. The rest take the page of the passage before them.
- **Fetching:** both copies are fetched once by `lampus/textbooks/fetch.py`, one page at a time with a pause, and never committed.

**Chunks cut by the book's structure:**

- **One chunk per unit of the book:**
  - an explanation under a heading;
  - a worked example, with the "Try It" boxes that practise it;
  - a "How To";
  - a group of exercises under one heading, up to 1,200 characters;
  - a part of the answer key.
- **Size:** blocks over 1,800 characters are split into pieces of about 1,200.
- **Context line:** each chunk starts with where it is ("Prealgebra 2e › 4.2 Multiply and Divide Fractions › Simplify Fractions › Example 4.19"), so it makes sense on its own.
- **Metadata:** each chunk carries its printed `page`, `page_end`, `page_label` (`"275"` or `"361-362"`), chapter, kind and title.
- **The answer key** is tagged `section: answers`. Search reads only `section: body` (the engine's filter since Step 6), so the tutor can't retrieve answers to exercises.

**One engine change:** `format_page()` shows `page_label` when a loader provides it, so every citation (the search tool, the search graph's labels) shows the printed page. Loaders without it are unchanged.

**Ingest:** `python -m lampus.ingest_textbook` writes the `prealgebra` collection through the Step 6 lineage sync, so re-running it embeds only what changed. `--fixed` builds the "before" collection with the engine's loader.

## Measurements

**The test set:**
- 30 student-style questions over chapters 4–6 (`lampus/evals/questions.jsonl`). Some use the book's terms ("What is a reciprocal?"), some don't ("How do I add fractions that have the same bottom number?").
- Each question carries a phrase from the book. Its expected page is the printed page where that phrase appears in the PDF, found independently of both loaders.
- A question counts as found when one of the top 5 results cites that page.

GitHub Codespace (2 cores), `bge-small-en-v1.5` embeddings, 2026-11-27, `lampus/bench/bench_retrieval.py`:

| Questions whose page is in the top 5 (of 30) | vector | keyword | hybrid | rerank (engine default) |
| --- | --- | --- | --- | --- |
| Before: engine loader, fixed chunks, PDF page cited | 0 | 1 | 1 | 0 |
| Before, with the page number corrected | 15 | 20 | 20 | 21 |
| **After: web text and math, book-structured chunks, printed pages** | **27** | 20 | 25 | 24 |

| | Before | After |
| --- | --- | --- |
| Answer-seeking questions with answer-key text in the top 5 | 5 of 5 | **0 of 5** |
| Chunks | 565 (answer key included as body text) | 1,129 (64 of them answer key, never searched) |
| Ingest time, chapters 4–6 | 144 s | 309 s |
| Search time p50, rerank | 892 ms | 1,303 ms |

**What the numbers say:**

- **The page number alone was the whole failure.** As cited, the before setup found 0 of 30 in its default mode. Correcting only the number gives 21. Every citation the tutor would have given was wrong.
- **Structure and math add the rest.** With the page number corrected in both, the default mode goes from 21 to 24 of 30, and vector search from 15 to 27.
- **Reranking doesn't help here.** With the new chunks, plain vector search (27) beat hybrid (25) and rerank (24), and the reranker costs 1.3 s per search on 2 cores.
  - The reranker (`ms-marco-MiniLM-L-6-v2`) was trained on web search questions, not short math questions against textbook passages.
  - Thirty questions is a small set: 3 questions apart is a hint, not proof. The default stays `rerank` for now, and the mode is re-measured with the bilingual questions in Step 12.
- **Some misses are the test's.** The expected page is where the chosen phrase appears. For "How do I find the area of a circle?" that's the section's opening on page 407, while the formula is probably on page 408, and a result there counts as a miss. Mishal will review the questions and their pages as part of the golden set (Phase 3).

16 new tests (`tests/test_lampus_textbook.py`), using HTML snippets and a three-page PDF made on the fly:

- **Math:** fractions, mixed numbers, brackets, powers and square roots are written out, and the duplicate MathML annotation is dropped.
- **Blocks:** an example keeps its math, its solution and its "Try It"; paragraphs under a heading stay together; videos are skipped; exercises are grouped; answers are parsed.
- **Pages:** printed numbers come from the footers, ranges from the table of contents, and passages are found by their words.
- **Chunks:** they cite printed pages, answers are set apart, and `format_page` prefers the printed label.

## Alternatives considered

- **OCR the PDF's math** (a formula-recognition model on each drawn formula): heavy for a 2-core Codespace, and it would give back a guess at math the web pages already have exactly.
- **Web pages only:** exact text and math, but no page numbers. Citations would be "section 4.2, Example 4.19". Correct, but not what a student holding the printed book looks for.
- **Fix only the page offset** (printed = PDF page − 10): works for this book, but the offset changes between books and around unnumbered pages, and it leaves the math and structure problems. The footers give the real number for every page.
- **Smaller fixed chunks** (500 characters): more precise, but examples still get cut, the math is still missing, and the answer key still leaks.
- **Leave the answer key out entirely:** simpler, but Phase 4's homework grading needs the answers. Keeping them tagged and unsearchable keeps them available to code that should see them.

## Consequences

- **License:** the book is CC BY-NC-SA 4.0, non-commercial. That's fine for the portfolio and a free pilot, but it must be swapped before Lampus charges anyone (`lampus/textbooks/README.md`, `data/SOURCES.md` in the course repo).
- **Re-fetching:** if OpenStax changes its web page layout, `section_blocks` needs updating. The tests pin the shapes it expects. The PDF and the web pages must be the same edition, or passages won't be found on their pages.
- **Bigger collection:** twice as many chunks, because each example, how-to and exercise group is its own chunk. Ingesting takes about twice as long (309 s for three chapters, once).
- **Next:** Step 12 measures questions in Urdu and Roman Urdu against these English chunks. Math-aware keyword search (keeping `3/4` and `x^2` as tokens) is left until keyword search proves worth it: it found 20 of 30 here, the fewest.
