# 0024. Two folders: engine and Lampus

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 2

## Context

The repository grew step by step: 31 Python files sat in the root, some of them early learning scripts (Steps 1–3, the Step 6 comparison) that nothing used any more. Lampus, a tutoring product, is now being built on this engine. Its code needs a clear home that doesn't blur the engine, which stays a general bot builder.

## Decision

- Move every engine module into `engine/`, a Python package. Imports become `from engine.bot_config import load_bot` / `from engine import step9_agent`. Commands run from the repository root as modules: `python -m engine.api`.
- Paths that meant "the repository root" now go up one more folder (`Path(__file__).resolve().parent.parent`), so `bots/`, `data/`, `documents/`, `chroma_db/` and `notes/` stay where they were.
- Move the seven files nothing imports (`step1_all_in_one`, `step1_search`, `step2_chatbot`, `step3a_manual_loop`, `step3b_langgraph_agent`, `step6_compare`, `prompts`) into `archive/steps/`, with their imports updated so they still run (`python -m archive.steps.step2_chatbot`).
- Create `lampus/` for the tutoring product. It holds only a README until Phase 2.
- Keep module names as they are (`step9_agent`, `step10_builder`) for now: renaming them to what they do is a separate, later change, so this move stays mechanical and easy to review.

## Alternatives considered

- **A separate Lampus repository that installs the engine as a package:** cleaner boundaries, but the engine would first need packaging and versioning, and every engine change would need a release. Revisit if Lampus grows a team of its own.
- **Keep files in the root and add `lampus/` beside them:** no migration, but the engine and the product would be hard to tell apart.
- **Delete the unused early steps:** they document how the project was learned; git history would keep them, but the README walks through them.

## Consequences

- Moved with `git mv`, so each file keeps its history.
- Checked: every module compiles and imports; the 69 tests pass; the CLIs (`engine.manage_users`, `evals/run_evals.py`) start; a load test through `python -m engine.api` completes every turn as fast as before the move (time to first token 1.37 s against 1.42 s at baseline, 1 student).
- Every command in the README and in module docstrings changed from `python api.py` to `python -m engine.api`.
- Older ADRs and the roadmap still name files without the `engine/` prefix; they describe the project as it was when written.
