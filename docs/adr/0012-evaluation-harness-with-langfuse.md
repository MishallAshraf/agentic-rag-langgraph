# 0012. Evaluation harness: golden dataset, layered scoring, Langfuse Cloud, a stronger judge

- Status: Accepted
- Date: 2026-11-23
- Step: 8

## Context

Steps 6 and 7 changed retrieval and added graders and a fact check, each judged on a handful of questions run by hand. That doesn't scale and can't catch regressions: a prompt change that fixes one answer can quietly break three others. The platform goal (ADR 0007) makes this worse, because generated bots will need automatic evaluation too (Step 10). We needed a repeatable way to say "this version is better than that one", plus a way to see what happened inside a run when a score is bad.

## Decision

- **A hand-checked golden dataset** (`evals/golden.yaml`), versioned in git. It holds 22 cases across covered, paraphrased, multi-part, partly covered, not covered, off-topic, follow-up, tool use with approval, and safety. Each case has the expected behaviour in words, key points, expected pages, forbidden text, and required or banned tool calls. Every fact was checked against the PDF.
- **Synthetic cases only with human approval.** `generate_candidates.py` has the judge model draft paraphrased questions from random passages. Drafts are saved with `approved: false` and ignored until a person checks them.
- **Layered scoring, cheapest first:**
  - retrieval: `page_hit`, `page_recall`
  - deterministic rules: `no_forbidden_text`, `trajectory`, `citations_valid` (every cited page was actually retrieved), `approval_respected`, `no_error`
  - LLM judge: `judge_correct` and `judge_faithful`, kept separate because they fail independently
  - `cost_usd` and `latency_s`
- **A stronger judge than the agent.** `anthropic/claude-sonnet-5.5` judges `anthropic/claude-haiku-5.5`, configurable with `EVAL_JUDGE_MODEL`. It uses `method="json_schema"`, because Sonnet 5.5 rejects the forced tool choice that `function_calling` needs. Calls are retried with backoff, and a judgement that still fails is recorded as `judge_error`, never silently dropped.
- **Calibrate the judge against a human.** `setup_review.py` puts a run into a Langfuse annotation queue with a boolean `human_correct` score. `calibrate.py` reports agreement and Cohen's kappa against `judge_correct`, and lists the disagreements.
- **Langfuse Cloud** handles tracing, datasets, experiments and annotation:
  - the dataset is upserted with the case id as the item id
  - each case runs through `dataset.run_experiment` with the LangChain callback handler, so its trace holds the whole graph (agent, tools, search subgraph, fact check) with the scores attached
  - without keys the same code runs locally through `Langfuse.run_experiment` and sends nothing
- **Local results and a gate.** Each run is saved as JSON in `evals/results/` (not committed) and compared with the previous run over the same cases. `--gate` exits 1 if any of these happens:
  - `judge_correct` falls below 0.80
  - `judge_faithful` falls below 0.85
  - `trajectory` falls below 0.90
  - `page_hit` falls below 0.85
  - any of those four drops by more than 0.10
  - any case has no judge score
- **Ablation flags** for comparing configurations on the same cases: `--no-verify`, `--retrieval-mode`, `--model`.

## Alternatives considered

- **LangSmith:** the native LangChain/LangGraph option with the same features, but it is closed source and cannot be self-hosted on a free plan. Langfuse is open source (MIT), can be self-hosted later on the same API, and its cloud free tier is enough for this project.
- **Arize Phoenix, or a fully local Langfuse in Docker:** both avoid a cloud account. Docker uses several GB of RAM for ClickHouse, Postgres and Redis, too much for this 8 GB machine next to the models. Langfuse Cloud now, self-hosting later, keeps the same code.
- **Ragas / DeepEval metrics:** ready-made faithfulness and relevance metrics, but they add a dependency stack and hide the judge prompt. Our own judge is about 50 lines, readable and changeable, and the deterministic checks cover what these libraries can't know (our tools, approval, citations).
- **The agent's own model as the judge:** cheaper (about 20 times less per token), but it shares the agent's blind spots, the self-preference problem.
- **Fully synthetic dataset:** fast to make, but an LLM writing the questions and grading the answers, with nobody in between, measures its own opinions.
- **Exact-match or embedding-similarity scoring of answers:** cheap, but free-text answers that are right in different words would fail, and fluent wrong answers can be similar to the reference.

## Observed (2026-11-23)

Baseline over 22 cases (`claude-haiku-5.5` agent, fact check on, rerank retrieval):

| Score | Value |
|---|---|
| judge_correct | 0.955 (21/22) |
| judge_faithful | 1.000 |
| page_hit / page_recall | 0.941 / 0.941 |
| trajectory, citations_valid, approval_respected, no_forbidden_text | 1.000 |
| agent cost per case / latency per case | about USD 0.001 / 9.3 s |

Comparison runs over the same 22 cases (each changes one setting from the baseline):

| Score | Baseline (rerank, fact check on) | Fact check off | Keyword retrieval |
|---|---|---|---|
| judge_correct | 0.955 | 0.955 | 0.955 |
| judge_faithful | 1.000 | 0.909 | 0.909 |
| page_hit / page_recall | 0.941 / 0.941 | 0.941 / 0.941 | 0.941 / 0.922 |
| no_forbidden_text | 1.000 | 1.000 | 0.955 |
| latency per case | 9.3 s | 7.7 s | 8.0 s |
| failing cases | hope-predicts-progress | hope-predicts-progress (incorrect); frank-vs-lambert, similar-outcomes-paraphrase (unfaithful) | relationship-importance (incorrect); emdr, similar-outcomes-paraphrase (unfaithful) |

- **The fact check earns its cost.** Without it, 2 of 22 answers contained claims beyond the sources: one-to-one mappings between Frank's and Lambert's lists that the text never makes, and "common factors matter more than the differences". It costs about 1.5 s per answer, and the cost per case barely moves.
- **Same averages, different failures.** Keyword retrieval found page 4 for the hope question (the word "hope" matches exactly) but missed page 15 for "how much does the relationship matter?". Correctness averages are identical, but the failing cases changed. Compare runs case by case, not only by their averages.
- **The offline evaluation caught what the runtime check missed.** In the keyword run, the EMDR answer claimed a passage "mentions" EMDR (none does) and added "eye movements or bilateral stimulation" from general knowledge. The Step 7 fact checker passed it; the judge and the forbidden-text rule both flagged it.
- **Cost per full run:** about USD 0.08 (agent and Sonnet judge together, measured from the OpenRouter balance), much less than estimated.

Two operational defects came out of the first attempts at these runs, which hung and then ran out of credit:
- `get_llm()` had no request timeout, and one run hung for over an hour on a request that never returned. It now has 60 s with two retries.
- The judge sent no `max_tokens`, so OpenRouter reserved credit for 65,536 output tokens (about USD 0.65 on Sonnet) on every call. The cap is now 2,000.

The one baseline failure (`hope-predicts-progress`) was a retrieval miss. The agent searched for "optimism", "expectancy" and "outcome", while page 4 says "hope is a good predictor of likely progress". The grader judged the passages it had as enough. The answer stayed honest about its sources: faithful but incorrect. It is kept as a known failure, not tuned away, to avoid fitting the system to a single case.

Building the harness found four defects that hand-testing had missed:
- concurrent searches could open the Chroma store twice (fixed with a lock in `retrieval.py`)
- the judge model rejected forced tool choice
- failed judgements vanished from the averages
- one test case's forbidden phrase was too strict (expanding "EMDR" is not answering from general knowledge)

## Consequences

- Every later change (Step 9 guardrails, caching, the model choice) can be measured against the same dataset before it is accepted, and the gate can run in CI.
- A run costs real money for the judge: about USD 0.08 for a full 22-case run, agent and judge together (measured from the OpenRouter balance; the harness counts only the agent's tokens). Ablations multiply that.
- Twenty-two cases are enough to find big regressions, but not small ones: one case is 4.5 percentage points. The dataset should grow through approved candidates, and differences of one or two cases should be read as noise.
- The judge stays unverified until a human grades a sample in the review queue and `calibrate.py` shows acceptable agreement.
- Traces and inputs go to Langfuse Cloud (EU region). That is fine for course questions, but must be revisited before any real user data flows through (Step 13).
- Revisit: when Langfuse self-hosting fits the hardware, when the dataset passes about 100 cases (add cost-aware sampling), and when Step 10 needs evaluations generated for new bots.
