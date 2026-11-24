# 0016. FastAPI with Server-Sent Events; streamed tokens are a preview, the final event is the answer

- Status: Accepted
- Date: 2026-11-24
- Step: 11

## Context

Every agent so far ran in a terminal. The React frontend (Step 12) needs an HTTP API, and that raises four problems:

- **Answers are slow.** A turn takes 5–30 s: input check, graded search, answer, fact check. Waiting in silence for a whole response feels broken.
- **Some turns pause in the middle.** A tool that writes (`save_note`) waits for a human decision, which may come much later.
- **Streaming conflicts with checking.** The fact check (ADR 0011) can reject an answer after it was written. Anything already streamed to the user may then be withdrawn.
- **The server is shared.** Concurrent requests, abusive clients, clients that disconnect mid-turn, and slow model calls all have to leave conversations valid.

## Decision

**FastAPI, with the turn streamed as Server-Sent Events (SSE).** These are the routes in `api.py`:

| Route | Purpose |
|---|---|
| `GET /bots` | Public fields only. The system prompt and tool setup never leave the server. |
| `POST /bots/{bot}/threads` | Start a conversation (a random 128-bit id). |
| `GET /bots/{bot}/threads/{thread}` | User and assistant messages, plus a pending approval. |
| `POST /bots/{bot}/threads/{thread}/messages` | Run a turn, stream its events. |
| `POST /bots/{bot}/threads/{thread}/approval` | Resume a paused turn with `approve` or `reject`, stream the rest. |

**The events in a stream:**
- `status`:
  - `received`, sent at once
  - the guard verdict
  - each tool call and result
  - the fact check result
- `token`: part of the answer, tagged with its message id.
- Then exactly one of:
  - `approval`: the turn paused
  - `done`: the final answer, its sources and the guard verdict

  An `error` event comes before `done` when the turn failed and was repaired.

**Tokens are a preview; `done` is authoritative.**
- Tokens are streamed only from the answering node (`agent`). The guard, grader and fact-check models are filtered out by the node name in the stream metadata.
- If the fact check rejects a draft, the revision streams under a new message id.
- The client replaces the preview with the `done` answer. This keeps the fact check without hiding all progress.

**An approval pause ends the stream.** The turn waits in the checkpointer (ADR 0009), not in an open connection. A separate request resumes it, so a decision can come minutes later, or from another device.

**Protection, in code:**
- **One turn at a time per conversation:** a lock per thread. A second message gets `409` instead of corrupting the history. So does a message sent while an approval is pending.
- **Rate limit:** a sliding window per client address (default 10 turns per minute). Extra requests get `429` with `Retry-After`.
- **Latency budget:** 120 s per turn on top of the 40-step limit (ADR 0014). On timeout, the Step 9 recovery repairs the conversation.
- **Client disconnects:** the turn is cancelled, and recovery runs protected from the cancellation. The conversation never keeps an unanswered tool call.
- **Validation:** message length at most 4000 characters, thread ids must be 32 hex characters, unknown bots return `404`.
- **Exposure:** CORS allows only the Vite dev origin. The server listens on `127.0.0.1` only, because there is no authentication until Step 13.

**Build graphs lazily.** Each bot's tools and graph are built on first use and kept, since an MCP server may have to start. A broken bot file is logged and skipped, not fatal.

## Alternatives considered

- **WebSockets.** Two-way and lower overhead per message, but this API only pushes in one direction. SSE is plain HTTP, so it works with standard proxies, logging and authentication headers, and every browser supports it. WebSockets earn their place with live collaboration or voice, not here.
- **Polling (`POST` returns a job id, the client polls).** Simple and robust to disconnects, but it adds latency equal to the polling interval and makes token streaming impractical. Worth having later for long background jobs, such as the bot builder.
- **Waiting for the fact check before streaming anything.** Never shows a rejected draft, but the user waits 5–10 s in silence. Rejected: streaming plus an authoritative final event gives both.
- **Keeping the connection open during an approval.** Simpler for the client, but a decision can take minutes and connections drop. Rejected in favour of a checkpointed pause.
- **A rate limit library or an API gateway.** Better for production. An in-process window shows the mechanism, and the server is a single process for now.

## Consequences

**Measured on this machine (Haiku 5.5, warm server):**
- First event at once (0.0 s). Before the `received` event was added, the first event arrived only when the input check finished, 1.4–6.6 s later.
- First answer token at 4.5 s; whole answer at 7.2 s. A longer answer streamed 90 token events.
- An approval resumed in a separate request and finished in 5.4 s.
- A client that hung up during a search left a repaired conversation. A follow-up on the same thread worked, and its search came from the cache the cut-off turn had filled.
- A concurrent message got `409`. The fourth request in a 3-per-minute window got `429`.
- The tests cost about USD 0.005 in total.

**Limits to revisit:**
- **Locks, the rate limit and built graphs live in one process.** Several workers or machines would need a shared store (Redis) for locks and limits, and sticky routing or stateless graphs.
- **Rate limiting by IP address** is wrong behind a proxy or a shared network. Step 13 should limit per user and per bot, with cost-based quotas.
- **No authentication:** anyone who can reach the port and knows a thread id can read that conversation. This is acceptable only on localhost (Step 13).
- **A turn whose client leaves is cancelled, not finished.** The user gets an apology on their next visit instead of the answer. Running turns as background jobs that outlive the connection would fix this, at the cost of a job store.
