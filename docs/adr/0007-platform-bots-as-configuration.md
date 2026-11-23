# 0007. Platform direction: bots defined by configuration, builder later

- Status: Accepted
- Date: 2026-11-23
- Step: 3

## Context

The goal is to grow from one chatbot into a chatbot creator: describe a use case, and get a bot with its own prompts, agents, tools, documents and appearance. Building a generator before the agent engine is solid would produce bots that cannot be judged, because there is no evaluation yet.

## Decision

- Keep Steps 4 to 9 focused on the engine, using the counselling study assistant ([ADR 0006](0006-product-counselling-study-assistant.md)) as the first bot.
- From Step 4, a bot is defined by a configuration file (name, theme, documents collection, agents with their prompts and tool lists, suggested questions) instead of Python constants. The engine loads whichever configuration it is given.
- Add a bot builder as Step 10: a configuration schema, a builder agent that writes a configuration from a plain-language description, one document collection per bot, and automatic evaluation of each generated bot.
- Tools come from a fixed registry of tested tools. The builder chooses tools; it never generates tool code.
- Appearance is theming (colours, name, avatar, welcome text, suggested questions, a few layout options), not generated interfaces.

## Alternatives considered

- **Switch to the platform now:** rejected; the builder would be built on an engine without evaluation, memory or hardening.
- **Stay a single-purpose bot:** simpler, but misses the multi-tenancy, access-control and evaluation-at-scale topics of the course.
- **Let the builder generate tool code:** rejected; it would let any bot creator run arbitrary code on the server.

## Consequences

- The roadmap gains one step (Steps 10 to 14 shift by one).
- Moving prompts and tools into configuration in Step 4 is a small cost now that makes Step 10 mostly additive.
- Later steps gain a multi-tenant dimension: per-bot document isolation and access control, per-bot quotas, per-bot themes in the frontend, and evaluation of bots nobody hand-wrote.
- Bot creators will upload their own documents, so prompt injection through retrieved text becomes a real risk to handle in Step 9.
