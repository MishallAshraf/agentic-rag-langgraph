# Architecture decision records

An ADR records one significant decision: the context, what was decided, the alternatives, and the consequences. Write one only for decisions that would be costly to reverse or that someone would reasonably question.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-chroma-as-vector-store.md) | Chroma as the vector store | Accepted |
| [0002](0002-local-embeddings-with-fastembed.md) | Local embeddings with fastembed (`bge-small-en-v1.5`) | Accepted |
| [0003](0003-recursive-chunking-1000-150.md) | Recursive character chunking, 1000 characters, 150 overlap | Accepted |
| [0004](0004-pin-all-dependency-versions.md) | Pin every dependency version | Accepted |
| [0005](0005-llm-provider.md) | OpenRouter as the LLM provider | Accepted |
| [0006](0006-product-counselling-study-assistant.md) | First product: a counselling study assistant | Accepted |
| [0007](0007-platform-bots-as-configuration.md) | Platform direction: bots defined by configuration, builder in Step 10 | Accepted |
| [0008](0008-human-approval-enforced-by-the-graph.md) | Human approval of side-effect tools, enforced by the graph | Accepted |
| [0009](0009-sqlite-checkpoints-and-summary-compaction.md) | SQLite for conversation state, with summarise-and-trim compaction | Accepted |
| [0010](0010-hybrid-retrieval-with-reranking.md) | Cleaner parsing, hybrid retrieval and reranking; keep Chroma | Accepted |
| [0011](0011-graded-retrieval-and-answer-checking.md) | Graded retrieval inside a tool, and a fact check on final answers | Accepted |
| [0012](0012-evaluation-harness-with-langfuse.md) | Evaluation harness: golden dataset, layered scoring, Langfuse Cloud, a stronger judge | Accepted |

Statuses: **Proposed** (under discussion), **Accepted**, **Superseded by NNNN** (replaced by a later decision; the old record is kept, not edited).

## Template

Copy this into a new file named `NNNN-short-title.md`:

```markdown
# NNNN. Title

- Status: Proposed
- Date: YYYY-MM-DD
- Step: N

## Context

What problem or force makes a decision necessary.

## Decision

What we chose.

## Alternatives considered

Each option and why it was not chosen.

## Consequences

What becomes easier, what becomes harder, and when to revisit.
```
