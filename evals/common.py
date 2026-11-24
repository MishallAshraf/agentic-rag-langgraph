"""Shared pieces of the evaluation harness: paths, the dataset, Langfuse, prices."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so evals/ scripts can import the project modules

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

EVALS_DIR = ROOT / "evals"
RESULTS_DIR = EVALS_DIR / "results"
GOLDEN_FILE = EVALS_DIR / "golden.yaml"
CANDIDATES_FILE = EVALS_DIR / "candidates.yaml"

DATASET_NAME = "study-assistant-golden"


def dataset_name(bot_id: str) -> str:
    """The Langfuse dataset for a bot: one per bot, so their results never mix."""
    return DATASET_NAME if bot_id == "study_assistant" else f"{bot_id.replace('_', '-')}-golden"


def langfuse_item_id(dataset: str, case_id: str) -> str:
    """The id of a case's item in Langfuse, where item ids must be unique across ALL datasets.

    Every generated bot starts with tests called gen-1, gen-2..., so the plain case id clashed as
    soon as a second bot was evaluated with Langfuse on. Other datasets get the dataset name in
    front; the study assistant's dataset came first and keeps its plain ids, so its run history
    stays linked to the same items.
    """
    return case_id if dataset == DATASET_NAME else f"{dataset}.{case_id}"
# A stronger model than the agent's, so the judge does not share the agent's blind spots.
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "anthropic/claude-sonnet-5.5")

# USD per million tokens (input, output), from OpenRouter's model list in November 2026.
PRICES = {
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
    "openai/gpt-4o-mini": (0.15, 0.60),  # the fallback model
}


def cost_usd(usage_by_model: dict) -> float | None:
    """Cost of a {model_name: usage} dict, or None if a model's price is unknown."""
    total = 0.0
    for model, usage in usage_by_model.items():
        price = next((p for name, p in PRICES.items() if name in model), None)
        if price is None:
            return None
        total += usage["input_tokens"] / 1e6 * price[0] + usage["output_tokens"] / 1e6 * price[1]
    return total


def load_cases(include_candidates: bool = True, dataset: Path | None = None) -> list[dict]:
    """Hand-written cases, plus generated candidates a human has approved.

    With dataset: that file only (Step 10: each generated bot has its own).
    """
    cases = yaml.safe_load((dataset or GOLDEN_FILE).read_text(encoding="utf-8"))
    if dataset is None and include_candidates and CANDIDATES_FILE.exists():
        candidates = yaml.safe_load(CANDIDATES_FILE.read_text(encoding="utf-8")) or []
        cases += [c for c in candidates if c.get("approved")]
    ids = [c["id"] for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SystemExit(f"Duplicate case ids: {sorted(duplicates)}")
    return cases


def to_item(case: dict) -> dict:
    """A case in the dataset-item shape Langfuse uses: input, expected_output, metadata."""
    return {
        "id": case["id"],
        "input": {"turns": case["turns"]},
        "expected_output": {
            "expected_behaviour": case.get("expected_behaviour", ""),
            "key_points": case.get("key_points", []),
            "expected_pages": case.get("expected_pages", []),
            "expected_sources": case.get("expected_sources", []),
            "must_not_say": case.get("must_not_say", []),
        },
        "metadata": {
            "category": case["category"],
            "must_call": case.get("must_call", []),
            "must_not_call": case.get("must_not_call", []),
            **({"approve": case["approve"]} if "approve" in case else {}),
            "source": "generated" if case.get("generated") else "hand-written",
            "case_id": case["id"],  # the YAML id, whatever id the item has in Langfuse
        },
    }


def get_judge_llm():
    """The judge model, with a cap on output tokens.

    Without max_tokens, OpenRouter reserves credit for the model's full output
    limit (65,536 tokens, about USD 0.65 on Sonnet) on every request, although a
    verdict needs a few hundred. With little credit left, requests were refused.
    """
    from llm import get_llm

    return get_llm(model=JUDGE_MODEL).model_copy(update={"max_tokens": 2000})


def langfuse_enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def get_langfuse():
    """The Langfuse client. Without keys it still works locally, but sends nothing."""
    from langfuse import Langfuse

    if not langfuse_enabled():
        return Langfuse(public_key="pk-lf-local", secret_key="sk-lf-local", tracing_enabled=False)
    client = Langfuse()
    if not client.auth_check():
        raise SystemExit("Langfuse rejected the keys in .env. Check LANGFUSE_PUBLIC_KEY, "
                         "LANGFUSE_SECRET_KEY and LANGFUSE_HOST.")
    return client
