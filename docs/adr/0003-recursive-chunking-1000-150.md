# 0003. Recursive character chunking, 1000 characters, 150 overlap

- Status: Accepted
- Date: 2026-10-10
- Step: 1
- Revisited in: [ADR 0010](0010-hybrid-retrieval-with-reranking.md) (Step 6), which keeps this decision

## Context

Whole pages are too broad to embed well: a page covers several topics, so its vector matches none of them sharply. Documents must be split into chunks that each hold one idea, small enough to keep prompts cheap later. There is no evaluation data yet to tune the split against.

## Decision

Use `RecursiveCharacterTextSplitter` with `chunk_size=1000`, `chunk_overlap=150` and `add_start_index=True`. It cuts at paragraph breaks first, then line breaks, then spaces, and repeats up to 150 characters between neighbouring chunks. The 22-page PDF produces 77 chunks. Every chunk is also written to `chunks/` for inspection.

## Alternatives considered

- **Fixed-size splitting on one separator (`CharacterTextSplitter`):** simpler, but cuts more often in awkward places.
- **Token-based splitting:** matches model limits exactly; unnecessary while chunks are far below any limit.
- **Sentence splitting (NLTK, spaCy):** never cuts a sentence, but needs an extra NLP dependency.
- **Structure-aware splitting by headings:** better chunks with section metadata, but needs a parser that outputs headings (for example Markdown); the current PDF loader gives plain text.
- **Semantic chunking:** cuts where the topic shifts, at the cost of an embedding call per sentence.

## Consequences

- A reasonable, widely used default that is easy to understand.
- Boilerplate such as footers and reference lists becomes its own chunks and can outrank real content; observed in Step 1.
- 1000 and 150 are starting values, not measured ones. Revisit in Step 6 with better parsing, and confirm with the evaluation harness from Step 8.
- Any change to these settings requires re-running ingestion.
