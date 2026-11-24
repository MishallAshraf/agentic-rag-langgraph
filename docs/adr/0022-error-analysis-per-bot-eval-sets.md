# 0022. Error analysis: each bot's evaluation set, failure modes and judges come from its own conversations

- Status: Accepted (labelling in progress)
- Date: 2026-11-24
- Step: 17

## Context

The project is turning into a product for companies that build chatbots, helping them test and improve their bots. Research for that product showed the open question isn't tooling. Promptfoo, Braintrust and others already exist, and the basic layers are free. LangChain's survey, cited by O'Reilly Radar, found that 89% of teams with production agents have observability but only 52% have evals. The gap is knowing *which* failures to measure for a given bot, and trusting the measurement.

The method in *Evals for AI Engineers* (Shankar and Husain, O'Reilly; its public FAQ at hamel.dev) starts from the application, not from metrics:
1. Generate or collect representative conversations.
2. A domain expert reads them and notes failures in their own words (open coding).
3. Group the notes into failure modes (axial coding).
4. Build one evaluator per mode and validate it against the expert's labels, using true positive and true negative rates.
5. Find the first step that failed.

Measured against that method, our evaluation had gaps:
- Test sets came from documents (Step 10) and attacks from 8 fixed categories (Step 16), so coverage was luck.
- No expert ever read the bot's output systematically.
- The judges used one general rubric, and only the study bot's judge had been checked against people (Step 8, kappa, now out of date).
- The improvement loop therefore trusted a judge nobody had validated for the bot it was changing.

## Decision

`analysis.py` and a review page in the web app (`#/review/<bot>`, admins only) implement the method. Every artefact is plain YAML or JSON in `evals/analysis/<bot id>/`, reviewed in git.

1. **Dimensions.** A planner (Sonnet) proposes 3-4 dimensions along which users differ, using the bot's topics and document excerpts. A person edits them. For the café bot, "urgency" was dropped because it overlapped with persona and produced unrealistic combinations; persona, intent and scenario remained, giving 216 combinations.
2. **Generate.** `sample_profiles` picks combinations so that each value appears about equally often, and repeats a combination only when there are more messages than combinations. An LLM then writes one realistic first message per profile, in batches, using real details from the documents. Choosing profiles first and wording second keeps coverage under our control, not the model's.
3. **Run.** Each message goes through the real Step 9 graph. The trace records:
   - the input check's verdict
   - every tool call and its result (with the data tags removed)
   - fact-check revisions, models and time
4. **Label (open coding).** The expert marks pass or fail. A failure needs a note in their own words and the first step that went wrong (input check, search, answer, fact check, other). Code enforces both: without the note there is nothing to code, and without the step the symptoms get blamed instead of the cause. Labels are written atomically and audited.
5. **Taxonomy (axial coding).** The planner groups the notes into 2-8 failure modes, each with a definition, and assigns each failure to modes. Code drops invalid parts and lists them:
   - unknown modes
   - assignments to conversations that passed
   - repeated ids

   The expert corrects the assignments on the page and approves. Approval is refused while any failure has no mode, and a later label reopens the review.
6. **Judges.** Labelled conversations are split train/dev/test (20/40/40), stratified by failure mode. Each mode gets a binary judge (Sonnet) that sees up to two expert-labelled failures and two non-failures from train only. TPR and TNR are measured on dev and test. On test, the expert's rate, the judge's raw rate and a **corrected rate** are reported. The correction uses the Rogan-Gladen estimator with the dev TPR and TNR: (observed + TNR - 1) / (TPR + TNR - 1). It is undefined when the judge is no better than chance.
7. **Report.** `report.md` shows the failure rate per dimension value, where failures start, the failure modes, and judge quality, with a warning when counts are small.

The LLM steps run from the command line. The page covers only the human steps: labelling, assigning, approving and reading results.

## Alternatives considered

- **Let an LLM label the conversations.** Faster, but it measures the model against itself, and the method's point is that the expert's judgement defines failure. An LLM is used where it's strong: proposing dimensions, wording messages, and a first draft of the groupings that a person corrects.
- **Fixed categories (OWASP, our 8 red-team categories).** Useful for security attacks, which Step 16 keeps. But a café's real failures ("promised the soup is vegan", "didn't send a severe allergy to staff") don't fit general categories, and categories chosen in advance hide failures nobody expected.
- **Random sampling or "write 50 varied questions".** LLMs cluster around typical cases. Balanced profiles guarantee that, for example, the non-native speaker with a terse message and the upset customer pushing a policy both appear.
- **Agreement (kappa) as the judge metric**, as in Step 8. One number hides which way the judge errs. TPR and TNR separate missed failures from false alarms, which have different costs, and they are what the correction formula needs.
- **A labelling tool (Label Studio, Argilla, a vendor platform).** Mature, but labels and traces would live outside the bot's repository and permissions, and the steps would need custom rendering anyway. A small page inside the admin app reuses accounts, auditing and the trace format.

## Consequences

- Café bot so far:
  - dimensions reviewed
  - 50 messages generated
  - 50 traces, all answered by Haiku, no errors, about 22 s each
  - Generating cost about USD 0.10.
- The next step needs a person: about 40-60 minutes of labelling. The taxonomy and judges come after that.
- With 50 conversations and perhaps 10-15 failures, each mode has only a handful of positives. The TPR and TNR are therefore rough, and the report says so. The method's answer is to keep labelling until new conversations stop revealing new kinds of failure ("saturation").
- This is the core of the product direction: a company's bot gets its own dimensions, failure taxonomy and validated judges. Step 18 makes it work on bots outside this codebase, over HTTP. Step 19 points the improvement loop's red team at the taxonomy and makes its acceptance depend on validated judges.
- Revisit:
  - running the LLM steps from the page as background jobs, like builds (ADR 0020)
  - multi-turn conversations, where the first failure can be several turns before the bad reply
  - code checks for modes that don't need an LLM, for example a price that isn't on the menu
