"""Benchmark vector search on the real bots: Chroma (before) vs Postgres + pgvector (after).

    python loadtest/bench_vectors.py --backend chroma
    python loadtest/bench_vectors.py --backend postgres     # uses DATABASE_URL

For every bot with eval questions (43 questions over 3 bots), it embeds each
question once, then times the vector search alone (top 10, body chunks only,
5 rounds) and checks the answers against an exact search: numpy comparing the
question with every chunk of that bot, which is slow but never misses.

    recall@10 = of the 10 truly nearest chunks, how many the store returned.

It also saves each store's top-10 ids, and once both backends have run, prints
how much the two stores' answers overlap. Results are appended to
loadtest/results.jsonl.
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.retrieval import BODY_ONLY  # noqa: E402
from engine.vectorstore import get_embeddings  # noqa: E402

RESULTS = Path(__file__).parent / "results.jsonl"
TOP_IDS = ROOT / "data" / "loadtest" / "vector_top10_{backend}.json"
K, ROUNDS = 10, 5
BOTS = {  # collection -> its eval questions
    "documents_clean": ROOT / "evals" / "golden.yaml",
    "bot_customer_handbook": ROOT / "evals" / "bots" / "customer_handbook.yaml",
    "bot_architecture_guide": ROOT / "evals" / "bots" / "architecture_guide.yaml",
}


def questions(path: Path) -> list[str]:
    return [case["turns"][-1] for case in yaml.safe_load(path.read_text(encoding="utf-8"))]


class ChromaBackend:
    def __init__(self, collection):
        from langchain_chroma import Chroma
        self.col = Chroma(collection_name=collection, persist_directory=str(ROOT / "chroma_db"))._collection

    def body_chunks(self):
        data = self.col.get(where=BODY_ONLY, include=["embeddings"])
        return data["ids"], np.array(data["embeddings"])

    def search(self, vector, k):
        return self.col.query(query_embeddings=[vector], n_results=k, where=BODY_ONLY, include=[])["ids"][0]


class PostgresBackend:
    def __init__(self, collection):
        from engine.vectorstore import get_vectorstore
        self.store = get_vectorstore(collection)

    def body_chunks(self):
        ids, vectors = self.store.embeddings_of(where=BODY_ONLY)
        return ids, np.array(vectors)

    def search(self, vector, k):
        return [d.id for d in self.store.similarity_search_by_vector(vector, k=k, filter=BODY_ONLY)]


def exact_top(chunk_ids, chunk_vectors, vector, k):
    v = np.asarray(vector)
    sims = chunk_vectors @ v / (np.linalg.norm(chunk_vectors, axis=1) * np.linalg.norm(v))
    return [chunk_ids[i] for i in np.argsort(-sims)[:k]]


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["chroma", "postgres"], required=True)
    args = parser.parse_args()
    backend_cls = {"chroma": ChromaBackend, "postgres": PostgresBackend}[args.backend]
    embedder = get_embeddings()

    latencies, recalls, top_ids = [], [], {}
    for collection, path in BOTS.items():
        backend = backend_cls(collection)
        chunk_ids, chunk_vectors = backend.body_chunks()
        qs = questions(path)
        vectors = [embedder.embed_query(q) for q in qs]
        backend.search(vectors[0], K)  # warm-up
        for q, vector in zip(qs, vectors):
            for _ in range(ROUNDS):
                start = time.perf_counter()
                got = backend.search(vector, K)
                latencies.append(time.perf_counter() - start)
            truth = exact_top(chunk_ids, chunk_vectors, vector, K)
            recalls.append(len(set(got) & set(truth)) / len(truth))
            top_ids[f"{collection}|{q}"] = got
        print(f"{collection}: {len(chunk_ids)} body chunks, {len(qs)} questions")

    summary = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "label": f"vector search: {args.backend}", "questions": len(top_ids),
               "search_p50_ms": round(pct(latencies, 50) * 1000, 2),
               "search_p95_ms": round(pct(latencies, 95) * 1000, 2),
               "recall_at_10": round(statistics.mean(recalls), 3)}
    print(json.dumps(summary))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")

    out = Path(str(TOP_IDS).format(backend=args.backend))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(top_ids, indent=1), encoding="utf-8")
    other = Path(str(TOP_IDS).format(backend="postgres" if args.backend == "chroma" else "chroma"))
    if other.exists():
        theirs = json.loads(other.read_text(encoding="utf-8"))
        shared = [len(set(top_ids[key]) & set(theirs[key])) / K for key in top_ids if key in theirs]
        print(f"Chroma and Postgres top-10 overlap: {statistics.mean(shared):.1%} over {len(shared)} questions")


if __name__ == "__main__":
    main()
