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
# A stronger model than the agent's, so the judge does not share the agent's blind spots.
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "anthropic/claude-sonnet-5.5")

# USD per million tokens (input, output), from OpenRouter's model list in November 2026.
PRICES = {
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
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


def load_cases(include_candidates: bool = True) -> list[dict]:
    """Hand-written cases, plus generated candidates a human has approved."""
    cases = yaml.safe_load(GOLDEN_FILE.read_text(encoding="utf-8"))
    if include_candidates and CANDIDATES_FILE.exists():
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
            "must_not_say": case.get("must_not_say", []),
        },
        "metadata": {
            "category": case["category"],
            "must_call": case.get("must_call", []),
            "must_not_call": case.get("must_not_call", []),
            **({"approve": case["approve"]} if "approve" in case else {}),
            "source": "generated" if case.get("generated") else "hand-written",
        },
    }


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
