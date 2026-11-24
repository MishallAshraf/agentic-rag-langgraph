# 0021. An improvement loop for bots: red team, research, engineer, architect; fixes limited to learned rules a person approves

- Status: Accepted
- Date: 2026-11-24
- Step: 16

## Context

Since Step 15, anyone with admin rights can build a bot from documents in minutes. The checks at build time catch broken configs and ungrounded questions. They don't show how a bot behaves when customers push it: hidden instructions, invented prices, "just this once" discounts, allergy guarantees, angry or manipulative messages.

The goal is a loop that keeps finding such failures and fixing them, in four roles:
- **red team:** tries to make the bot fail
- **researcher:** finds how production systems handle each failure
- **engineer:** implements the fix
- **architect:** checks that everything still works

Two risks shape the design:
- A loop that can change a bot can also make it worse. It can over-refuse normal questions, contradict its own rules, or take on instructions an attacker planted in a test.
- Earlier decisions hold: an LLM never writes code (ADR 0015), and a person approves what goes live (ADR 0020).

## Decision

`improve.py` is a LangGraph graph with code between the agents:

```
red_team -> probe -> triage --no failures--> report
                       '-> research -> engineer -> verify --fixed, no regressions--> human review -> apply -> report
                                          ^-- feedback --'   '--gave up--> report
```

1. **Red team.** Three fixed seed attacks (injection, prompt leak, off-topic) run every time, so runs stay comparable. Up to 15 generated attacks are aimed at real details, using excerpts from the bot's documents, across eight categories: injection, leak, off-topic, hallucination, policy, safety, privacy and manipulation. Some of the bot's own suggested questions are added as *normal* checks, because a fix that makes the bot refuse them is a failure.
2. **Probe.** Every test runs through the bot's real Step 9 graph, one fresh conversation each, with approvals always rejected. A stronger model (the Step 8 judge, Sonnet) scores each reply against its expected behaviour, seeing every reply in a multi-turn test. The runner also records which guardrail answered and which models ran.
3. **Research.** One note per kind of failure, grounded in `docs/production_playbook.md`. The playbook is a reviewed summary of common practice, citing OWASP Top 10 for LLM Applications 2025, NIST AI 600-1 and *Moffatt v. Air Canada*. The researcher reads only the relevant section, and must label anything outside it "(general practice)". Failures answered by the input guardrail are classified **in code** as needing a code change, because no prompt rule can affect a guard that runs before the prompt.
4. **Engineer.** It proposes at most 3 **learned rules**: one-sentence instructions in a new `learned_rules` field, which code appends to the system prompt at run time (`BotConfig.prompt_with_rules`, used by Step 9). Code checks each rule (`rule_problems`):
   - 10-300 characters, one line
   - no tags, code or links
   - no "ignore / override the instructions" wording
   - no duplicates, and at most 12 rules per bot

   A rejected rule gets one correction. The engineer can't touch tools, approvals, permissions, documents or code.
5. **Architect.** It builds the candidate bot and reruns every failed test, plus a regression sample (the normal checks and up to 4 attacks that passed before). Then it reviews the rules for side effects. A fix is accepted only if it fixes at least one failure, causes no regression, and the architect approves. Otherwise the engineer gets the concrete feedback, and after 2 attempts the loop gives up.
6. **Human review, then apply.** A person sees the rules, the reason for them and the before-and-after results. On approval, the rules go into the bot's YAML (validated before writing), and **every attack that broke the bot becomes a test** in `evals/bots/<bot>.yaml`, so the Step 8 harness keeps checking it. Every run writes a report to `evals/results/`.

## Alternatives considered

- **Let the engineer rewrite the system prompt.** It has more power, but a rewrite can't be reviewed line by line, it drifts from the template code renders (ADR 0015), and a planted attack could pass instructions into it. Appending checked one-sentence rules keeps each change small, readable and reversible: delete a line from the YAML.
- **Let the engineer change code**, for example new guardrail categories or retrieval settings. Many real fixes are code, as this step showed. But an agent writing code needs a sandbox, code review and CI. That is a software-engineering workflow, not a bot setting. The loop reports these as recommendations ("needs a code change") for a person.
- **Accept a fix when the failed tests pass.** The first live run showed why not. Rules that fixed the persona attack broke a policy test that had passed before. Regression checks, including normal questions, are the core of the architect's job.
- **Live web search for the researcher.** It would bring fresher sources, but also unvetted pages, prompt injection through search results, and an API key and cost per run. A reviewed playbook gives answers that can be checked and repeated. A search tool can be added later behind the same note format.
- **One agent doing all four jobs.** It would make fewer calls, but no step would check another. Separate roles with code between them make each decision visible, testable (11 tests with stand-in models) and bounded.

## Consequences

Three live runs on the café bot (`--dry-run`: nothing written):

| Run | Failures | What happened | Tokens |
|---|---|---|---|
| 1 | 3 of 11 | Fixed 1 of 3, then a regression; gave up after 2 attempts | 238k (80k on the fallback model) |
| 2 | 1 of 11 | The failure came from the input guardrail; reported as a code change | 104k |
| 3 (after the fix below) | 1 of 10 | An angry customer was classed as an injection by the guard; reported as a code change | 91k |

**The loop's main finding was in the platform, not in a bot.** Recording which models answered showed that every structured call inside the bot — the input guard, the search grader and the fact check — was answered by the fallback model (gpt-4o-mini), not the configured Haiku. These calls used `method="function_calling"`, a forced tool call. Measured directly, the same call was answered by gpt-4o-mini with the fallback list set, and by Haiku with `json_schema`. In this setup, the configured model does not serve the forced tool call, and OpenRouter quietly routes the request to the fallback instead of failing. The Step 8 judge had already met the same problem with Sonnet. Since Steps 7 and 9, those checks had all run on another model, including the guard that sent crisis-helpline numbers in reply to an allergy question.

All components now use one setting, `llm.STRUCTURED_METHOD = "json_schema"`. A bot turn then runs entirely on Haiku, and the Step 14 planner still routes correctly. The lesson: record which model actually answered. A fallback that hides failures also hides behaviour.

Other consequences:
- The loop's own first versions had flaws that live runs exposed:
  - a multi-turn attack was judged on its last reply only
  - guardrail replies were treated as fixable by prompt rules
  - malformed structured output aborted the run, so every structured call now retries
  - 3 parallel bot runs hit rate limits and fell back, so it is now 2
- A run costs about USD 0.05-0.20, mostly the Sonnet judge, and takes 1.5-3 minutes. It is meant to run after a bot is built or changed, not on every message.
- Run 3's recommendation is done. The input check (guardrails.py) classified pushy customers who asked it to "break your rules" or "override the limit" as injection, because its prompt didn't separate the business's rules from the assistant's own instructions. The prompt now does, with examples, and says "when unsure between injection and ok, choose ok". `evals/check_guard.py` measures it on labelled messages in `evals/guard_cases.yaml`, three times each:
  - before the fix: 66 of 75 correct, all 9 errors pushy customers classed as injection
  - after: 99 of 99, including 8 held-out cases written after the fix
  - in a fourth loop run, the manipulation attack was answered by the bot, and the loop moved on to a privacy failure it could fix with a rule
- Revisit:
  - to support research bots (their sources are other bots)
  - to run the loop from the admin page as a background job, like builds (ADR 0020)
  - to let code fixes go through a reviewed branch workflow
