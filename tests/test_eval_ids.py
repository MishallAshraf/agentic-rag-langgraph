"""Langfuse dataset item ids must be unique across every bot's dataset (no network, no LLM).

    python -m unittest tests.test_eval_ids -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from common import DATASET_NAME, dataset_name, langfuse_item_id, to_item  # noqa: E402
from run_evals import case_id_of, sync_dataset  # noqa: E402

CASE = {"id": "gen-1", "category": "paraphrased", "turns": ["Which dishes contain nuts?"]}


class StandInLangfuse:
    """Stores items the way Langfuse does: an id may belong to one dataset only."""

    def __init__(self):
        self.owner = {}

    def create_dataset(self, name, description):
        pass

    def create_dataset_item(self, dataset_name, id, **_):
        if self.owner.setdefault(id, dataset_name) != dataset_name:
            raise RuntimeError(f"Dataset item id {id} already exists in another dataset")


class LangfuseItemIds(unittest.TestCase):
    def test_two_bots_with_the_same_case_ids_can_both_sync(self):
        langfuse = StandInLangfuse()
        for bot_id in ("architecture_guide", "customer_handbook"):
            sync_dataset(langfuse, [CASE], dataset_name(bot_id), "tests")  # raised before the fix
        self.assertIn("customer-handbook-golden.gen-1", langfuse.owner)
        self.assertIn("architecture-guide-golden.gen-1", langfuse.owner)

    def test_study_assistant_keeps_its_plain_ids(self):
        self.assertEqual(dataset_name("study_assistant"), DATASET_NAME)
        self.assertEqual(langfuse_item_id(DATASET_NAME, "gen-1"), "gen-1")

    def test_results_map_back_to_yaml_ids(self):
        item = to_item(CASE)
        self.assertEqual(case_id_of(item), "gen-1")  # Langfuse off: plain dicts
        remote = SimpleNamespace(id="customer-handbook-golden.gen-1", metadata=item["metadata"])
        self.assertEqual(case_id_of(remote), "gen-1")  # Langfuse on: dataset items
        old = SimpleNamespace(id="gen-1", metadata=None)  # an item synced before case_id existed
        self.assertEqual(case_id_of(old), "gen-1")


if __name__ == "__main__":
    unittest.main()
