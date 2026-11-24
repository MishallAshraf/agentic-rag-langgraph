# 0019. Multi-agent research bots: planner, parallel researchers, writer, one review round; agents act with the user's permissions

- Status: Accepted
- Date: 2026-11-24
- Step: 14

## Context

Each bot so far answers from one collection with one agent loop. Some questions span knowledge bases, for example "How does the study assistant handle crisis messages, and what does the course say about responding to clients at risk?". The first half is in the design records, the second in the course material.

A single agent could be given both search tools, but it would then:
- search one source after the other
- keep every passage from every search in one growing context
- decide on its own when it has covered every part of the question

The course's deep research case study answers this with a team: one agent plans, several research in parallel, one writes and one checks.

Step 13 adds a constraint. Each user may read only the bots they were granted. A team that searches other bots' collections must not become a way around that.

## Decision

A bot can have a `research` section instead of documents of its own (`bot_config.Research`):

```yaml
research:
  sources: [study_assistant, architecture_guide]   # other bots whose collections it may search
  max_tasks: 3                                      # sub-questions per turn (1 to 5)
  review_rounds: 1                                  # reviewer passes (0 to 2)
```

`step14_research.py` builds a LangGraph graph with four roles:

1. **Planner:** splits the latest question into at most `max_tasks` self-contained sub-questions and picks one source for each. The output is structured (`Plan`). Code then drops unknown or forbidden sources, removes duplicates and caps the count. If nothing usable is left, each source is asked the question as it is.
2. **Researchers:** one per sub-question, started together with `Send`. Each runs the Step 7 graded search on its source and summarises the passages with citations, or says that nothing was found (`Finding`). Each sees only its own sub-question and its own passages.
3. **Writer:** combines the findings into one answer with the bot's hardened system prompt. Findings arrive inside `<tool_output>` tags as data. The sources behind the answer go in `response_metadata`, so the API and the frontend show them as they do for every bot.
4. **Reviewer:** checks that the answer covers every part of the question and that every claim is in the findings (`Review`). Gaps send new researchers, but not for a sub-question already searched. Unsupported claims send the draft back to the writer. The rejected draft is removed from the conversation. After `review_rounds`, the answer stands.

The team sits inside the same Step 9 guardrails as every other bot: `step9_agent.build_graph(..., make_assistant=...)`. So crisis and injection checks, the canary and the output check still apply.

**Permissions are inherited, never added.** For each turn, the API puts the sources this user may use into `config["configurable"]["allowed_sources"]`. The planner is only told about those sources. Each researcher checks again before it searches, because a plan is model output and is never trusted for access. A user with no usable source gets a fixed reply and the turn makes no model call at all. A research bot cannot list itself as a source.

## Alternatives considered

- **One agent with every source's search tool.** It is simpler and cheaper per turn, but searches are sequential, and every passage stays in one context that grows with each search. Nothing checks that every part of the question was answered. It also makes permissions a matter of which tools to build per user, rather than one rule checked in two places.
- **A supervisor agent that calls workers as tools, in a loop.** This is more flexible, because the supervisor can decide what to do after each result. But the order of work becomes model behaviour: it is harder to test, harder to bound in cost, and the workers run one at a time unless the supervisor asks for several. A fixed plan, research, write and review flow is enough for research questions, and every path through it is covered by a test.
- **An unbounded review loop ("until the reviewer approves").** Its cost has no ceiling, and a reviewer that keeps finding something would never stop. One round fixes most problems. `review_rounds` allows up to two.
- **Letting researchers write the final answer together** (each appends a section). The answer reads as stitched together and repeats itself. One writer gives one voice and one place where the citation rules apply.
- **Sending findings in the conversation history.** The next turn would carry every passage from every earlier turn. Findings live in their own state key and are cleared at the start of each turn. Only the final answer stays in the conversation.

## Consequences

Measured on the suggested crisis question (OpenRouter, Claude Haiku 5.5):

| | Single-agent turn | Research turn (first live run, misrouted) | Research turn (after the fix) |
|---|---|---|---|
| Tokens | about 5,000 | 22,365 | 9,245 |
| Time to answer | about 13 s | 40 s | 19 s |

A research turn costs about twice the tokens and time of a single-agent turn when it goes well, and about 4.5 times when the reviewer sends it round again. It is worth it for questions that span sources, not for simple ones. That is why research is a separate kind of bot, not a mode of every bot. The daily token quota from Step 13 counts every agent's calls.

**The source descriptions are the routing prompt.** In the first live run, the planner sent "how does the study assistant handle crisis messages?" to the source *named* Study Assistant (the course material), not to the design records that describe how the assistant works. The reviewer repeated the mistake, and every finding came back "nothing found". The writer said so honestly, but the answer was useless.

Two fixes, in order of weight:
1. The design records' bot now says what its documents describe: "the design records (ADRs) of this project, which builds the counselling study assistant and its bot platform", with crisis handling in its topics. After this change, all three test questions routed correctly. Before it, one of the three still misrouted even with the improved prompt.
2. The planner sees each source's contents first and its display name last, and is told that a name in the question is not a reason to choose a source with a similar name.

A routing error is invisible in the answer's tone, because the answer is honest about finding nothing. So routing needs its own check. A planner-only check (the planner prompt alone, on a few questions) is a small fraction of a full run's cost.

The reviewer also flagged "the course does not cover this" as an unsupported claim, which caused a pointless rewrite. Its prompt now says that reporting a "Not found" finding is supported.

Other consequences:
- Orchestration and permissions are tested without a model (`tests/test_research.py`, 9 tests). A scripted model plays every role, and search is replaced. The tests also check that the researchers run in parallel: three 0.4 s searches take less than 0.8 s.
- The frontend shows the plan, each researcher's result and the review as steps. Only the writer's tokens stream as the draft.
- Revisit when sources number more than a handful. The planner then needs a retrieval step over source descriptions, not a list in its prompt. Also revisit if users ask follow-ups that need the earlier findings: those findings are cleared each turn on purpose.
