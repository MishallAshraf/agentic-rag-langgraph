# 0011. Graded retrieval inside a tool, and a fact check on final answers

- Status: Accepted
- Date: 2026-11-23
- Step: 7

## Context

Until Step 6 the agent retrieved once and trusted the result. Irrelevant passages still reached the model (costing tokens and inviting confusion), a weak first query was never retried unless the agent happened to search again, and nothing checked that the final answer stayed within the sources. In Step 4 the agent recovered a cut-off list only because it chose to call `read_page`.

## Decision

- **Corrective retrieval as a subgraph behind one tool.** `search_course_material(question, query)` runs a small graph (`search_graph.py`): retrieve, then an LLM grader returns which passages are relevant, whether they are enough, and a rewritten query if not. Up to 3 attempts; it stops early when the grader has no new query. Only relevant passages are returned, with a search log and a status line ("answer", "partly answer", or "no passage answers this").
- **A fact check node in the agent graph.** When `verify_answers` is on in the bot configuration and the turn used a source tool, `check_answer` compares the final answer with that turn's sources using a structured `Verdict`. An unsupported answer is removed from the history, the checker's objections are passed to the agent, and it answers once more. The second answer is not re-checked.
- Both graders use structured output (`with_structured_output(..., method="function_calling")`).
- Enabled for the study assistant by editing `bots/study_assistant.yaml`: the tool swap and the check needed no change to the engine's tool code.

## Alternatives considered

- **Grading and rewriting as nodes in the main agent graph** (the classic agentic RAG layout): more visible in one diagram, but the loop would have to be threaded through tool-call bookkeeping, and every bot would carry the retrieval loop in its main graph. As a tool, any bot can opt in through configuration.
- **Grade each passage with a separate LLM call:** finer-grained, but four calls per search instead of one.
- **Ask the agent in the prompt to double-check itself:** the same model that made the mistake judging its own answer within the same reply; not an independent check.
- **Check answers repeatedly until they pass:** risks loops and unbounded cost. One revision is the budget.

## Observed (2026-11-23, `anthropic/claude-haiku-5.5`)

| Question | LLM calls | Outcome |
|---|---|---|
| Lambert's four factors | 4 (agent, grader, agent, checker) | Grader kept 2 of 4 passages, enough on the first attempt; answer supported |
| CBT techniques for panic disorder (not covered) | 6 (agent, 3 graders, agent, checker) | Three differently worded searches, partial match only; the answer said what the material lacks |
| Stubbed failing check (test) | 5 | Failed answer removed, agent revised once, no second check |

A direct test of the checker passed a faithful answer and rejected one with invented claims, including a "30% of outcome" figure that is well known but not in the material.

## Consequences

- Answers rest on filtered evidence, and "the material doesn't cover this" now comes from an explicit grader verdict rather than the agent's impression.
- Cost and latency rise: a covered question went from 2 to 4 LLM calls, and an uncovered one can take 6. The agent's own input shrinks a little because irrelevant passages are dropped (2024 input tokens on the second agent call for the Lambert question, against about 2400 with ungraded search).
- The grader's rewritten queries draw on the model's general knowledge (for example "interoceptive exposure"), which helps find material phrased differently but can chase concepts the course never mentions.
- The checker is itself an LLM and can be wrong in both directions; its accuracy should be measured against human judgement in Step 8 (the course's "LLM-as-judge, calibrated against humans").
- The Step 5 engine reads the same bot file, so it also uses the graded search, but it does not run the fact check, which only `step7_agent.py` implements.
