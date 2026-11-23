# 0010. Cleaner parsing, hybrid retrieval and reranking; keep Chroma

- Status: Accepted
- Date: 2026-11-23
- Step: 6
- Revisits: [ADR 0001](0001-chroma-as-vector-store.md), [ADR 0003](0003-recursive-chunking-1000-150.md)

## Context

Steps 1 to 5 showed three retrieval problems: pypdf output contained words split by stray spaces; repeated page footers pulled reference-list chunks to the top; and a question about "active listening" missed the one passage using that exact term. Changes were measured with `step6_compare.py`: 12 questions, each with a phrase only the right passage contains, scored by hit@4 and MRR.

## Decision

- **Parsing:** PyMuPDF instead of pypdf (split words: about 198 down to about 5); page footers removed; reference lists tagged `section: references` and excluded by a metadata filter. Written to a new collection, `documents_clean`, leaving Step 1's collection for comparison.
- **Retrieval:** vector search and BM25 keyword search, merged with Reciprocal Rank Fusion, then the top 10 candidates reranked by a cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2` through fastembed). The agent's `search_documents` tool uses this pipeline.
- **Vector store:** keep Chroma. BM25 runs in Python over the same chunks, built in memory at start-up.
- **Chunking:** unchanged (1000 characters, 150 overlap), so the effect of the retrieval changes could be measured on its own.

## Measurements (12 questions, top 4)

| Method | hit@4 | MRR | Average latency |
|---|---|---|---|
| Step 1 vector (old parsing) | 0.83 | 0.79 | 21 ms |
| Vector (clean parsing) | 0.75 | 0.69 | 17 ms |
| Keyword (BM25) | 1.00 | 0.85 | under 1 ms |
| Hybrid (RRF) | 0.83 | 0.73 | 18 ms |
| Hybrid + rerank, 20 candidates | 1.00 | 0.90 | 1324 ms |
| Hybrid + rerank, 10 candidates | 1.00 | 0.90 | 669 ms |

## Alternatives considered

- **Keyword search alone:** matched reranking on hit@4 and costs almost nothing, but ranks the right passage lower (MRR 0.85) and would fail on paraphrased questions that share no words with the text; this test set has few of those.
- **Hybrid without reranking:** fusion was worse than BM25 alone here, because the weaker vector ranking diluted good keyword results. RRF weights both methods equally.
- **A vector database with built-in hybrid search (Qdrant, Weaviate):** removes the separate in-memory BM25 index, but means migrating stores for a 73-chunk corpus. Revisit if the corpus grows or several processes need the same index.
- **A larger reranker (`bge-reranker-base`, about 1 GB):** likely more accurate, but slower on this CPU, and the small one already ranks the right chunk first or second.

## Consequences

- Retrieval found the right passage for all 12 questions, and the Step 5 miss ("active listening") is fixed in the agent.
- Each search now costs about 0.7 s on this machine, versus about 20 ms before. That is small next to an LLM call (1 to 5 s), but it adds up when the agent searches several times per question.
- **The test set is tiny:** one question is worth 0.08 hit@4. The ranking of methods may not hold on a larger set; the golden dataset in Step 8 should re-check this decision.
- Cleaning PDF text did not improve pure vector search on this set (0.83 to 0.75). Its main benefits are elsewhere: readable chunks for the LLM, no footer noise, and a reliable section tag.
- The BM25 index must be rebuilt whenever the collection changes; at present it is rebuilt at every start-up.
- PyMuPDF is licensed under the AGPL, which matters if this code is ever distributed or offered as a service; a commercial licence or a different parser would then be needed.
- The Lambert list is still split across two chunks; the agent's `read_page` tool covers this, and structure-aware chunking remains an option.
