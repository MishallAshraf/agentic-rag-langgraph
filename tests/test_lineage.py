"""Lampus Phase 1, Step 6 - Data lineage and incremental ingestion (course topic 22).

    python -m unittest tests.test_lineage -v

A fake embedding model counts how many texts it is asked to embed, so these
tests need no model download. They check that unchanged files cost nothing,
that an edit re-embeds only the chunks whose text changed, that deleted files
disappear, that new settings redo everything, and that every chunk can be
traced back to its file and run.
"""

import secrets
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests._env  # noqa: E402, F401  (throwaway schema)

from langchain_core.documents import Document  # noqa: E402

from engine import lineage, vectorstore  # noqa: E402
from engine.vectorstore import ROOT, VectorStore  # noqa: E402


class FakeEmbeddings:
    def __init__(self):
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += len(texts)
        return [[float(len(t) % 7 + 1)] + [0.0] * 383 for t in texts]


def chunk_file(path: Path) -> list[Document]:
    """One chunk per paragraph."""
    chunks, start = [], 0
    for part in path.read_text(encoding="utf-8").split("\n\n"):
        if part.strip():
            chunks.append(Document(page_content=part.strip(),
                                   metadata={"source": path.name, "section": "body", "start_index": start}))
        start += len(part) + 2
    return chunks


def chunk_id(chunk: Document) -> str:
    return f"{chunk.metadata['source']}:{chunk.metadata['start_index']}"


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEmbeddings()
        self._real = vectorstore._embeddings
        vectorstore._embeddings = self.fake  # get_embeddings() returns the fake from now on
        self.folder = ROOT / "data" / f"test_lineage_{secrets.token_hex(3)}"
        self.folder.mkdir(parents=True)
        (self.folder / "a.md").write_text("Apples are red.\n\nPears are green.\n\nPlums are purple.", encoding="utf-8")
        (self.folder / "b.md").write_text("Trains run on rails.\n\nBuses run on roads.", encoding="utf-8")
        self.collection = f"lineage_{secrets.token_hex(3)}"

    def tearDown(self):
        vectorstore._embeddings = self._real
        shutil.rmtree(self.folder, ignore_errors=True)

    def sync(self):
        files = sorted(self.folder.glob("*.md"))
        return lineage.sync(self.collection, self.folder, files, chunk_file, chunk_id, loader="test")

    def test_unchanged_files_cost_nothing(self):
        first = self.sync()
        self.assertEqual((first["files_changed"], first["chunks_embedded"], first["chunks"]), (2, 5, 5))
        again = self.sync()
        self.assertEqual((again["files_changed"], again["chunks_embedded"], again["chunks"]), (0, 0, 5))
        self.assertEqual(self.fake.calls, 5)

    def test_edit_reembeds_only_changed_chunks(self):
        self.sync()
        (self.folder / "a.md").write_text("Apples are red.\n\nPears are yellow.\n\nPlums are purple.",
                                          encoding="utf-8")
        stats = self.sync()
        self.assertEqual((stats["files_changed"], stats["chunks_embedded"], stats["chunks_reused"]), (1, 1, 2))
        texts = [d.page_content for d in VectorStore(self.collection).get()]
        self.assertIn("Pears are yellow.", texts)
        self.assertNotIn("Pears are green.", texts)

    def test_deleted_file_disappears(self):
        self.sync()
        (self.folder / "b.md").unlink()
        stats = self.sync()
        self.assertEqual((stats["files_removed"], stats["chunks"]), (1, 3))

    def test_new_settings_redo_everything(self):
        self.sync()
        old = lineage.PIPELINE_VERSION
        lineage.PIPELINE_VERSION = old + 1
        try:
            stats = self.sync()
        finally:
            lineage.PIPELINE_VERSION = old
        self.assertEqual((stats["files_changed"], stats["chunks_embedded"], stats["chunks_reused"]), (2, 5, 0))

    def test_chunk_traces_back_to_file_and_run(self):
        run = self.sync()["run"]
        info = lineage.chunk_lineage(self.collection, "a.md:0")
        self.assertEqual((info["source_path"], info["run"], info["file_now"]), ("a.md", run, "unchanged"))
        self.assertEqual(info["settings"]["loader"], "test")
        (self.folder / "a.md").write_text("Apples are green now.", encoding="utf-8")
        self.assertEqual(lineage.chunk_lineage(self.collection, "a.md:0")["file_now"], "edited since this run")

    def test_chunks_without_lineage_are_replaced(self):
        store = VectorStore(self.collection)
        store.add_embedded(["old"], ["copied from Chroma"], [{"section": "body"}], [[1.0] + [0.0] * 383])
        self.sync()
        self.assertNotIn("old", [d.id for d in store.get()])
        self.assertEqual(store.count(), 5)


if __name__ == "__main__":
    unittest.main()
