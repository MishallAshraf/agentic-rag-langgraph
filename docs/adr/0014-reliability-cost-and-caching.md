# 0014. Reliability and cost limits; an exact cache instead of a semantic one

- Status: Accepted
- Date: 2026-11-24
- Step: 9

## Context

Step 8's evaluation runs exposed how the system behaves when things go wrong:
- one run hung for over an hour on a request that never returned
- with a low balance, requests were refused because each call reserved credit for the model's full 64k-token output
- the CLI crashed on any model error, and a crash between a tool call and its result could leave the saved conversation unusable

Each answer also costs 4–6 LLM calls (Step 7), so caching was worth examining.

## Decision

- **Timeout and retries:** 60 s per request, 2 retries (`llm.py`), plus backoff retries on the judge.
- **Output ceiling:** `max_tokens=2000` on every call. The longest answers are about 800 tokens.
- **Fallback model:** OpenRouter's `models` list, the main model first, then `OPENROUTER_FALLBACK_MODELS` (default `openai/gpt-4o-mini`, a different vendor). OpenRouter already falls back across providers of the same model; this covers the model itself being unavailable. `none` turns it off.
- **Step limit per turn:** `recursion_limit=40` graph steps (a normal turn takes 6–15), so a runaway tool loop stops before it burns money.
- **Recovery:** any failure in a turn is caught. The conversation is repaired (open tool calls answered with an error), the student gets a short apology, and the turn is recorded as finished so the next message works.
- **Exact cache for graded searches:** an in-process LRU cache (256 entries) of `search_course_material` results, keyed on the question lowercased and stripped of punctuation. It is turned off in evaluation runs so cases stay independent.
- **No semantic cache and no prompt caching**, for the reasons measured below.

## Alternatives considered

- **Semantic cache (embedding similarity above a threshold):** measured with the project's embedding model (`bge-small-en-v1.5`):

  | Pair | Similarity | Should hit? |
  |---|---|---|
  | "Does the relationship matter more than technique?" vs the reverse | 0.992 | No |
  | "What are common factors of change?" vs "...specific factors..." | 0.936 | No |
  | Lambert's factors, reworded | 0.957 | Yes |
  | Dodo bird effect, reworded | 0.966 | Yes |

  A wrong pair scores higher than every right one, so no threshold works. A semantic cache would serve the answer to a different question, with full confidence. A cross-encoder check or an LLM equivalence check on candidate hits could make it safe, at the cost of most of the saving.
- **Caching final answers:** answers depend on the conversation (follow-ups, the summary), and a cached wrong answer would be repeated to everyone. Caching the graded search instead keeps the answer fresh while skipping the expensive part.
- **Prompt caching (Anthropic `cache_control` via OpenRouter):** every call reported `cache_read: 0`. The prompts are 900–1,900 tokens, below the minimum length Anthropic models cache. Worth enabling for bots with long system prompts or many tools.
- **A persistent cache (SQLite, Redis):** survives restarts and is shared between processes, but needs invalidation when documents are re-ingested. In-process is enough for one CLI user; revisit with the API in Step 11.
- **LangChain `with_fallbacks`:** wraps the model in a runnable that loses `bind_tools`, and duplicates what OpenRouter does in one request.

## Observed (2026-11-24)

- **Exact cache:** the same question in a new conversation took 6.7 s instead of 10.7 s, with 4 LLM calls instead of 5. A search that needs three grading rounds saves three calls.
- **Fallback:** requests carrying the fallback list are accepted and served by the main model. An unknown main model id is rejected before routing (HTTP 400), so the failover itself could not be triggered cheaply and is untested here.
- **Fallback quality** (`gpt-4o-mini` as the whole agent, 8 cases): 7/8 correct and 7/8 faithful. It padded Lambert's factors with explanations not in the course, and didn't say the rejected note was not saved. Usable as a degraded mode, clearly weaker.
- **Recovery:** with a fake model, a tool crash (as an MCP server dying would cause) and a runaway loop (`GraphRecursionError`) both ended in the apology, and the next message in the same conversation was answered normally. Because the agent runs as a subgraph, a failed turn's partial messages never reached the main conversation at all.
- **Credit reservations:** with the cap, cases still failed with HTTP 402 at a USD 0.21 balance when run three at a time, and succeeded one at a time. OpenRouter holds back more than the capped estimate while requests are in flight.

## Consequences

- Failures now degrade, they don't crash: a timeout, then retries, then other providers, then the fallback model, then an apology with a usable conversation.
- A fallback answer may be weaker without the student knowing. The model that served each call is in the trace (`model_name`), so its share can be monitored.
- The cache is per process: it empties on restart and is not shared. Re-ingesting documents requires a restart to avoid stale results.
- Keep a balance margin on OpenRouter; parallel evaluation runs need more than their actual cost.
