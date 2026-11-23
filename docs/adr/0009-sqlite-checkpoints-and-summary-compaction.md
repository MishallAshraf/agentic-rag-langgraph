# 0009. SQLite for conversation state, with summarise-and-trim compaction

- Status: Accepted
- Date: 2026-11-23
- Step: 5

## Context

Conversations must survive restarts, and a pause for human approval must survive too ([ADR 0008](0008-human-approval-enforced-by-the-graph.md)). Until Step 4 the history lived in a Python list and was re-sent each turn, and input tokens grew without limit: retrieved passages stay in the history, so a few questions already reach several thousand tokens.

## Decision

- Use LangGraph's `AsyncSqliteSaver`, storing checkpoints in `data/checkpoints.sqlite`. Each conversation is a thread, with the id prefixed by the bot id (`study_assistant:abc123`) so bots cannot read each other's conversations.
- Each turn sends only the new message; the checkpointer supplies the history.
- A `compact` node runs at the start of each turn. When the history exceeds about 4000 tokens, every turn except the last two is summarised by the LLM into a `summary` field and removed from the messages. Cuts happen only at the start of a turn, so a tool call is never separated from its result. Tool results are left out of the summary.

## Alternatives considered

- **In-memory checkpointer:** no setup, but everything is lost when the process ends.
- **PostgreSQL checkpointer:** the production choice when several API workers must share state, but needs a database server. SQLite allows one writer at a time and lives on one machine.
- **A document database (NoSQL):** a checkpoint is essentially a JSON document per thread, so this would fit, but LangGraph's maintained checkpointers are SQL-based and nothing here needs a document database's flexibility.
- **Trim only (drop old messages without summarising):** free and simple, but the assistant forgets what was discussed.
- **Summarise everything each turn:** loses the exact wording of recent turns and costs a summarisation call every time.

## Consequences

- Conversations and pending approvals survive restarts; `--thread` resumes and `--list` lists them.
- Compaction costs one extra LLM call when triggered and loses detail: the summary keeps the gist and page citations, not the retrieved passages, so the agent may need to search again.
- The 4000-token threshold is set low to demonstrate compaction; a real setting depends on the model's context window and cost targets, and should be tuned with evaluation (Step 8).
- When the API runs several workers (Step 11), move to the PostgreSQL checkpointer; the graph code does not change, only the saver.
- Checkpoints store full conversation text on disk, which matters for privacy and retention once there are real users (Step 13).
