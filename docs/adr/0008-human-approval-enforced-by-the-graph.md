# 0008. Human approval of side-effect tools, enforced by the graph

- Status: Accepted
- Date: 2026-11-23
- Step: 4

## Context

From Step 4 the agent has tools that change things outside the conversation (`save_note` writes a file). Later bots will have tools with bigger consequences, such as sending messages or updating records. A model can call such a tool by mistake, or be pushed into it by text in a retrieved document (prompt injection).

## Decision

- Each bot configuration lists `approval_required` tools.
- After the `agent` node, a routing function sends any message containing such a tool call to a `human_approval` node, which pauses the graph with LangGraph's `interrupt()`. The graph resumes with the human's decision: approve runs the tools; reject answers every tool call in that message with a rejection, and the agent continues.
- Pausing needs a checkpointer; an in-memory one is used, with a fresh thread per question until Step 5.

## Alternatives considered

- **Ask for approval in the system prompt ("confirm with the user before saving"):** rejected. The model decides whether to comply, so it is not a control; it fails exactly when the model is confused or manipulated.
- **`interrupt_before=["tools"]` at compile time:** pauses before every tool, including harmless searches, which makes the agent tedious to use.
- **Check approval inside each tool function:** spreads the policy across tools, and does not work for tools served by MCP servers, whose code we do not control.

## Consequences

- The policy is enforced in code at one point in the graph, and applies equally to local and MCP tools.
- Approval is per bot, which fits the bot-creator platform ([ADR 0007](0007-platform-bots-as-configuration.md)).
- A rejection rejects all tool calls in the same message, even harmless ones. Acceptable for now; per-call decisions can be added if needed.
- The approval prompt is a terminal `input()` for now; the frontend (Step 12) will show approve and reject buttons, resuming the same graph through the API.
