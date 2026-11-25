"""Lampus Phase 1, Step 5 - The pgvector store (course topic 20).

    python -m unittest tests.test_vectorstore -v

Uses hand-made 384-number vectors, so no embedding model is downloaded. Checks
the promises retrieval relies on: nearest chunks come first, the metadata
filter (body chunks only) works, and one bot can never see or wipe another
bot's chunks.
"""

import secrets
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests._env  # noqa: E402, F401  (throwaway schema)

from engine.vectorstore import VectorStore  # noqa: E402

DIM = 384


def unit(*weights) -> list[float]:
    """A vector pointing mostly along the first few axes, e.g. unit(1, 0.2)."""
    v = [0.0] * DIM
    for i, w in enumerate(weights):
        v[i] = float(w)
    return v


class VectorStoreTest(unittest.TestCase):
    def setUp(self):
        tag = secrets.token_hex(3)
        self.a, self.b = VectorStore(f"bot_a_{tag}"), VectorStore(f"bot_b_{tag}")
        self.a.add_embedded(
            ["near", "middle", "far", "refs"],
            ["about apples", "about pears", "about trains", "reference list"],
            [{"section": "body", "page": 1}, {"section": "body", "page": 2},
             {"section": "body", "page": 3}, {"section": "references", "page": 9}],
            [unit(1, 0.1), unit(1, 1), unit(0, 0, 1), unit(1)])
        self.b.add_embedded(["other"], ["bot b's chunk"], [{"section": "body"}], [unit(1)])

    def test_nearest_first_and_filtered(self):
        got = self.a.similarity_search_by_vector(unit(1), k=3, filter={"section": "body"})
        self.assertEqual([d.id for d in got], ["near", "middle", "far"])  # "refs" is closest but filtered out
        self.assertEqual(got[0].page_content, "about apples")
        self.assertEqual(got[0].metadata, {"section": "body", "page": 1})  # JSON numbers stay numbers

    def test_bots_are_isolated(self):
        self.assertNotIn("other", [d.id for d in self.a.similarity_search_by_vector(unit(1), k=10)])
        self.assertEqual([d.id for d in self.b.similarity_search_by_vector(unit(1), k=10)], ["other"])
        self.a.reset_collection()
        self.assertEqual(self.a.count(), 0)
        self.assertEqual(self.b.count(), 1)

    def test_get_and_reingest(self):
        self.assertEqual([d.id for d in self.a.get(where={"section": "body"})], ["far", "middle", "near"])
        # Ingesting a chunk again replaces it instead of adding a duplicate.
        self.a.add_embedded(["near"], ["about green apples"], [{"section": "body"}], [unit(1, 0.1)])
        self.assertEqual(self.a.count(), 4)
        self.assertEqual(self.a.similarity_search_by_vector(unit(1), k=1, filter={"section": "body"})[0].page_content,
                         "about green apples")

    def test_stored_vectors_round_trip(self):
        ids, vectors = self.a.embeddings_of(where={"section": "references"})
        self.assertEqual(ids, ["refs"])
        self.assertEqual(vectors[0], unit(1))


if __name__ == "__main__":
    unittest.main()
