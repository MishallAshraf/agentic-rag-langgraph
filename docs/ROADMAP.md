# Roadmap

The goal is a chatbot creator: describe a use case and get a bot with its own prompts, agents, tools, documents and theme ([ADR 0007](adr/0007-platform-bots-as-configuration.md)). Steps 4 to 9 build the engine on one bot, the counselling study assistant; Step 10 adds the builder.

Each step builds on the previous one and maps to topics from the "Mastering AI system design" course. When a step is finished, its findings go into the course notes (kept locally, not in the repository) and any significant decision gets an [ADR](adr/README.md).

| Step | Status | Builds | Course topics |
|---|---|---|---|
| 1 | Done | Ingestion and plain retrieval | Embeddings; document ingestion; vector databases and ANN indexes; RAG chunking; data quality |
| 2 | Done | First LangGraph graph with an LLM | Tokens and context windows; model selection; streaming and TTFT |
| 3 | Done | Retrieval as a tool: a hand-written agent loop first, then the LangGraph version | Tool use; agent architectures and harnesses; structured outputs |
| 4 | Done | Bot configuration file; more tools from a tool registry, including one via MCP; human approval of tool calls | Tool use and MCP; tool security; agent permissions and human approval |
| 5 | Done | Memory across turns | Memory and long-session compaction; SQL vs NoSQL (checkpoint store) |
| 6 | Done | Better retrieval | Hybrid search; reranking; HNSW settings; parsing PDFs, tables, OCR |
| 7 | Done | Agentic RAG: grade results, rewrite the query, retry, cite sources | Agentic RAG; context engineering |
| 8 | Done | Evaluation harness and tracing | Golden datasets; synthetic data; LLM-as-judge; agent trajectory evaluation; tracing |
| 9 | Done | Hardening | Timeouts, retries, fallbacks; semantic and prompt caching; cost modelling; guardrails; prompt injection |
| 10 | Done | Bot builder: configuration schema, builder agent, one document collection per bot, automatic evaluation of generated bots | Structured outputs; context engineering; multi-tenancy; evals for generated bots |
| 11 | Done | Backend API (FastAPI, streaming over Server-Sent Events) | API design; streaming; rate limiting; latency budgets |
| 12 | Next | React frontend (Vite, React, TypeScript), themed per bot | Streaming UX; showing sources and agent steps; human approval UI |
| 13 | | Authentication and authorization, per user and per bot | Retrieval access control; per-tenant quotas; audit logging |
| 14 | | Multi-agent bots | Multi-agent orchestration; deep research case study |

## Principles carried through every step

- **Learn the mechanism before the abstraction.** Where a framework hides a loop or a pattern, build the bare version first (for example, the agent loop in Step 3).
- **Permissions are enforced in code, never by the LLM.** A prompt instruction is not an access control; retrieval filters and tool allow-lists are.
- **Measure before tuning.** Retrieval settings chosen in Step 1 are revisited once the evaluation harness exists.

## Out of scope on this machine

Training and adaptation (fine-tuning, preference tuning, distributed training) and model-serving internals (vLLM, continuous batching, KV cache) need a GPU. Their effects are observed and discussed in the course notes, but not built. The machine has 8 GB RAM and no GPU.

## Prerequisites still to install

- Node.js LTS, needed from Step 12.

## Open decisions

None at the moment. (Off-topic questions: decided in Step 4 that the study assistant declines them.)
