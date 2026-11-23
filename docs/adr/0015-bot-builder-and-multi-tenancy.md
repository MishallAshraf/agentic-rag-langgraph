# 0015. Bot builder: LLM fills fields, code renders and checks, a human approves; one collection per bot

- Status: Accepted
- Date: 2026-11-24
- Step: 10

## Context

[ADR 0007](0007-platform-bots-as-configuration.md) set the goal: a chatbot creator, where bots are configuration and tools come from a fixed registry. Until Step 10, the engine was configuration-driven but in practice single-tenant:
- one document collection (`documents_clean`)
- one notes folder
- prompts that mentioned counselling (the guard, the fact checker, the summariser, the judge)
- a search tool hard-wired to the study assistant's subject

A generated bot raises two separate questions:
- **Isolation:** can one bot see another bot's documents, notes or cached answers?
- **Trust:** can an LLM write a configuration that the server then runs, safely and well?

## Decision

**Multi-tenancy by construction:**
- Each bot has a `knowledge` section: a folder, its own Chroma collection and a subject.
- Tools are built per bot. `search_knowledge` and `read_page` are bound to that bot's collection and folder, and the notes MCP server is started with that bot's notes folder (`notes/<bot id>/`).
- The search cache key includes the collection.
- The model never chooses a collection or a path, so it cannot reach another bot's data however it is prompted.
- Every value that ends up in a path or a collection name is validated strictly:
  - ids: `^[a-z][a-z0-9_]{2,39}$`
  - folders: relative and inside the project
- Shared prompts (guard, fact check, summary, judge) take the bot's name, description, subject and topics instead of naming counselling.

**Tool policy in code (`check_policy`), for hand-written and generated bots alike:**
- only registered tools
- every tool that writes must need approval
- search tools need a knowledge section
- `read_page` needs PDFs in the folder

A breach stops the bot from loading.

**The builder (`step10_builder.py`), a LangGraph graph:**

1. ingest → survey: index the documents and show the LLM what they cover.
2. draft: the LLM (Sonnet 5.5) fills a structured `BotSpec`. This is a set of fields: role, audience, subject, topics, suggested questions, tone, tools, colour and avatar. It is not free text.
3. validate, in code:
   - the schema and the tool policy
   - 3 to 6 topics
   - colour contrast of at least 4.5 against white (WCAG AA)
   - every suggested question is grounded: the reranker finds a passage that scores at least 0 for it

   On failure, the draft goes back with the errors, at most twice.
4. write_tests: 4 domain tests drawn from sampled chunks, with the expected source and key points. A "not covered" test is added only when code confirms the term appears in no chunk. Three platform tests are always added: off-topic, injection and crisis.
5. review: a human approves, edits or rejects the proposal (`interrupt`).
6. save: `bots/<id>.yaml` and `evals/bots/<id>.yaml`, validated again.

**The system prompt is rendered by code from a template**, from the approved fields. The LLM never writes the prompt itself, so the safety-relevant parts (cite sources, decline off-topic, say "not covered") are always present and worded the same way.

**Generated bots are evaluated like the hand-written one:** `run_evals.py --bot … --dataset …`. The judge is told the bot's own purpose and topics.

## Alternatives considered

- **Let the LLM write the whole YAML, including the system prompt.** Fewer moving parts, but every bot's safety wording would depend on one generation, and a manipulated document could steer it. Rejected: the LLM chooses content, code owns structure and rules.
- **One shared collection with a `bot_id` metadata filter.** Fewer Chroma collections, but isolation then depends on every query remembering the filter, and the BM25 index (built per collection) would mix tenants. Separate collections make a missing filter impossible. Revisit if there are hundreds of bots, where a collection per bot becomes costly.
- **No human review; publish whatever validates.** Validation checks form and grounding, not judgement. For example, the first draft offered the notes tools, which were harmless but not wanted. Review costs one decision and keeps a person accountable for what goes live.
- **Generate all tests with the LLM, including "not covered".** The LLM proposed out-of-scope terms that the documents do mention. A test that expects "not covered" for a covered term teaches the wrong thing, so code now checks the term against every chunk before keeping it.

## Consequences

**Measured on the first generated bot**, the "Design Decisions Guide" over this project's ADRs:
- The builder run cost about USD 0.01, and the first draft passed validation.
- Correct: 8/8. Faithful: 8/8. Trajectory: 8/8.
- Source hit: 3/4 at section level. The miss retrieved the right ADR but neighbouring sections; the answer still had every key point.
- After the refactor, the study assistant's regression set still passed 4/4.

**Isolation checked without an LLM:**
- The same question returned only `Psychotherapy.pdf` for the study bot and only ADR 0014 for the guide.
- An unknown tool, a write tool without approval, `read_page` on Markdown, a folder outside the project, a path-like id and a low-contrast colour were each refused.

**The evaluation itself had to become per bot.** The first run marked a correct off-topic refusal as wrong because the judge still believed every bot was the counselling assistant. Any prompt shared across bots needs the bot's own context.

**The judge prompt wording changed for the study assistant too.** Its meaning is the same, but the Step 8 judge-versus-human agreement was measured on the old wording. Re-run `evals/calibrate.py` when credit allows.

**Section-level source labels are stricter than page labels.** A Markdown section is shorter than a PDF page, so a near miss is common. Consider counting the right file as a partial hit.

**Limits:**
- Formats are PDF, DOCX and Markdown only.
- Ingestion is synchronous.
- Bots, collections and notes live on one machine.
- Nothing yet stops a user from running another user's bot; that is authentication and authorisation (Step 13).
