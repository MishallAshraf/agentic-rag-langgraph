"""How much work does re-ingesting a bot's documents take when little has changed?

    python loadtest/bench_ingest.py                    # uses DATABASE_URL

Copies the architecture guide's documents (docs/adr, 28 Markdown files) to a
scratch folder and ingests them into a scratch collection three times:

  1. first ingest     everything is new
  2. no change        the same files again (e.g. a nightly re-index)
  3. one file edited  one ADR gets an extra paragraph

For each run it records how many chunks were embedded and how long the run
took. Embedding is the expensive part of ingestion: on a CPU it dominates the
time, and with a paid embedding API it is the bill. Results are appended to
loadtest/results.jsonl.
"""

import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.bot_config import Knowledge  # noqa: E402
from engine.ingest import ingest  # noqa: E402
from engine.vectorstore import get_vectorstore  # noqa: E402

RESULTS = Path(__file__).parent / "results.jsonl"
SCRATCH = ROOT / "data" / "loadtest" / "ingest_bench"
COLLECTION = "bench_ingest"
EDITED = "0010-hybrid-retrieval-with-reranking.md"


def run(knowledge, name):
    start = time.perf_counter()
    stats = ingest(knowledge, quiet=True)
    seconds = time.perf_counter() - start
    # Before lineage, ingest() embedded every chunk on every run.
    embedded = stats.get("chunks_embedded", stats["chunks"])
    row = {"run": name, "chunks": stats["chunks"], "chunks_embedded": embedded, "seconds": round(seconds, 1)}
    print(json.dumps(row))
    return row


def main():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    shutil.copytree(ROOT / "docs" / "adr", SCRATCH)
    knowledge = Knowledge(folder=str(SCRATCH.relative_to(ROOT)).replace("\\", "/"),
                          collection=COLLECTION, subject="benchmark copy of the ADRs")
    get_vectorstore(COLLECTION).reset_collection()
    rows = [run(knowledge, "first ingest"), run(knowledge, "no change")]
    with (SCRATCH / EDITED).open("a", encoding="utf-8") as f:
        f.write("\n\nRevisited in Lampus Phase 1: the store moved to Postgres with pgvector, "
                "and hybrid retrieval with reranking is unchanged.\n")
    rows.append(run(knowledge, "one file edited"))
    get_vectorstore(COLLECTION).reset_collection()
    shutil.rmtree(SCRATCH, ignore_errors=True)

    summary = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "label": "ingest bench", "files": len(list((ROOT / "docs" / "adr").glob("*.md"))), "runs": rows}
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    main()
