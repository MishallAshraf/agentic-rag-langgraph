# Architecture decision records

An ADR records one significant decision: the context, what was decided, the alternatives, and the consequences. Write one only for decisions that would be costly to reverse or that someone would reasonably question.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-chroma-as-vector-store.md) | Chroma as the vector store | Superseded by [0027](0027-pgvector-as-vector-store.md) |
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
| [0013](0013-guardrails-around-the-agent.md) | Guardrails around the agent: input check, tool output as data, canary | Accepted |
| [0014](0014-reliability-cost-and-caching.md) | Reliability and cost limits; an exact cache instead of a semantic one | Accepted |
| [0015](0015-bot-builder-and-multi-tenancy.md) | Bot builder: LLM fills fields, code renders and checks, a human approves; one collection per bot | Accepted |
| [0016](0016-fastapi-with-server-sent-events.md) | FastAPI with Server-Sent Events; streamed tokens are a preview, the final event is the answer | Accepted |
| [0017](0017-react-frontend-themed-per-bot.md) | React frontend: one theme variable per bot, drafts shown as drafts, approvals as cards, no HTML from the model | Accepted |
| [0018](0018-authentication-authorization-quotas-audit.md) | Server-side sessions, per-bot grants, private conversations, daily quotas, hash-chained audit log | Accepted |
| [0019](0019-multi-agent-research-team.md) | Multi-agent research bots: planner, parallel researchers, writer, one review round; agents act with the user's permissions | Accepted |
| [0020](0020-bot-builder-as-a-service.md) | The bot builder as a service: checked uploads, a background job with resumable progress, review in the browser, no restart | Accepted |
| [0021](0021-improvement-loop-red-team-research-fix-verify.md) | Improvement loop: red team, research, engineer, architect; fixes limited to checked learned rules a person approves | Accepted |
| [0022](0022-error-analysis-per-bot-eval-sets.md) | Error analysis: each bot's eval set, failure modes and judges come from its own conversations, labelled by a domain expert | Accepted |
| [0023](0023-run-on-linux-codespaces-and-ci.md) | Run on Linux: Windows-only packages behind platform markers, a Codespaces dev container, tests in CI on every push | Accepted |
| [0024](0024-engine-and-lampus-folders.md) | Two folders: `engine/` (the bot builder, a Python package) and `lampus/` (the tutoring product); unused early steps archived | Accepted |
| [0025](0025-postgres-for-users-quotas-and-audit.md) | Postgres with a connection pool for users, sessions, quotas and the audit log; quota and audit writes made atomic | Accepted |
| [0026](0026-conversations-in-postgres.md) | Conversations (LangGraph checkpoints) in Postgres on their own pool, with a checked migration from SQLite | Accepted |
| [0027](0027-pgvector-as-vector-store.md) | Document chunks in Postgres with pgvector; exact search per bot, HNSW measured | Accepted |
| [0028](0028-data-lineage-and-incremental-ingestion.md) | Data lineage (file hashes, ingest runs) and incremental ingestion | Accepted |
| [0029](0029-redis-for-shared-limits-locks-and-cache.md) | Redis for rate limits (token buckets per student and bot), conversation locks and the search cache | Accepted |
| [0030](0030-build-queue-and-worker.md) | Bot builds through a Redis Streams queue to a separate worker; progress in Postgres, pub/sub to notify | Accepted |
| [0031](0031-scale-out-nginx-and-read-replica.md) | Three API copies behind nginx, a read replica for searches, renewed 15 s conversation locks; a hang under load fixed | Accepted |
| [0032](0032-api-v1-idempotency-and-signed-webhooks.md) | The API under /v1, idempotency keys for resent requests, signed webhooks to schools (Standard Webhooks) | Accepted |

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
