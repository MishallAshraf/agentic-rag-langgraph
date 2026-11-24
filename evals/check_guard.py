"""Check the input guardrail against labelled messages (evals/guard_cases.yaml).

    python evals/check_guard.py            # every case
    python evals/check_guard.py --repeat 3 # each case 3 times, to see unstable ones

Prints each mismatch, a confusion table and which models answered.
Exits with status 1 if any case is classified wrongly. About 22 small calls.
"""

import argparse
import asyncio
import sys
from collections import Counter
from types import SimpleNamespace

from common import EVALS_DIR, cost_usd  # also loads .env and puts the project on sys.path

import yaml  # noqa: E402
from langchain_core.callbacks import UsageMetadataCallbackHandler  # noqa: E402

from guardrails import make_input_check  # noqa: E402
from llm import get_llm  # noqa: E402

CASES = EVALS_DIR / "guard_cases.yaml"
CATEGORIES = ("ok", "injection", "crisis")


def load():
    data = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    assistants = {k: SimpleNamespace(**v) for k, v in data["assistants"].items()}
    return assistants, data["cases"]


async def main(repeat: int) -> int:
    assistants, cases = load()
    usage = UsageMetadataCallbackHandler()
    llm = get_llm()
    llm.callbacks = [usage]  # on the model itself, so structured-output wrappers keep it
    checks = {k: make_input_check(llm, bot) for k, bot in assistants.items()}
    sem = asyncio.Semaphore(4)

    async def run(case):
        async with sem:
            return case, await checks[case["assistant"]](case["text"])

    results = await asyncio.gather(*(run(c) for c in cases for _ in range(repeat)))
    table, wrong = Counter(), 0
    for case, (got, reason) in results:
        table[(case["expect"], got)] += 1
        if got != case["expect"]:
            wrong += 1
            print(f"WRONG {case['id']}: expected {case['expect']}, got {got} ({reason})")

    print(f"\n{len(results) - wrong}/{len(results)} correct\n")
    print("expected \\ got  " + "  ".join(f"{c:>9}" for c in CATEGORIES))
    for e in CATEGORIES:
        print(f"{e:>14}  " + "  ".join(f"{table[(e, g)]:>9}" for g in CATEGORIES))
    cost = cost_usd(usage.usage_metadata)
    print(f"\nModels: {', '.join(usage.usage_metadata) or 'none recorded'}"
          + (f"; about USD {cost:.4f}" if cost is not None else ""))
    return 1 if wrong else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=1)
    sys.exit(asyncio.run(main(parser.parse_args().repeat)))
