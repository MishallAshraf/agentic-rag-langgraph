"""HNSW vs exact search at a size where the index matters (synthetic chunks, labelled as such).

    python loadtest/bench_hnsw.py                      # 50,000 chunks over 50 bots, uses DATABASE_URL
    python loadtest/bench_hnsw.py --chunks 20000 --bots 20

Lampus's real bots hold a few hundred chunks, where Postgres simply compares the
question with every chunk of the bot (exact, about 2 ms). This builds a bigger
table in its own schema (bench_hnsw) to see what the HNSW index buys and costs
once there are many schools.

SYNTHETIC DATA: every vector is one of the 318 real chunk embeddings plus random
noise, normalised, so it keeps the clumpy shape of real text embeddings without
embedding 50,000 texts on a laptop. Each vector is given to a random bot. The
questions are the 43 real eval questions.

Three kinds of search, for every question:

  whole table  top 10 over all chunks: exact (read every row) vs HNSW at
               several ef_search values (how many candidates HNSW keeps).
  one bot      top 10 among one bot's chunks (2% of the table), the query
               Lampus actually runs:
                 exact-filtered  find the bot's rows with the primary key, compare each
                 hnsw-filtered   walk the HNSW graph, then drop other bots' rows
                 hnsw-iterative  same, but keep walking until 10 rows survive
                                 (pgvector 0.8+, hnsw.iterative_scan)

recall@10 = of the 10 truly nearest chunks, how many the search returned.
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import psycopg
import yaml
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.db import DATABASE_URL  # noqa: E402
from engine.vectorstore import get_embeddings, get_pool, to_vector  # noqa: E402

RESULTS = Path(__file__).parent / "results.jsonl"
SCHEMA, K, ROUNDS = "bench_hnsw", 10, 3
EF_VALUES = [10, 20, 40, 80, 160, 320]
NOISE = 0.035  # per-dimension noise; a real embedding's values are about 0.05 each (illustrative choice)
QUESTION_FILES = [ROOT / "evals" / "golden.yaml", *sorted((ROOT / "evals" / "bots").glob("*.yaml"))]


def questions() -> list[str]:
    return [case["turns"][-1] for f in QUESTION_FILES for case in yaml.safe_load(f.read_text(encoding="utf-8"))]


def real_embeddings(conn) -> np.ndarray:
    rows = conn.execute("SELECT embedding::text FROM public.chunks").fetchall()
    if not rows:
        raise SystemExit("No chunks in public.chunks. Run python -m engine.migrate_chroma (or ingest) first.")
    return np.array([[float(x) for x in r[0].strip("[]").split(",")] for r in rows], dtype=np.float32)


def build(conn, n_chunks: int, n_bots: int):
    rng = np.random.default_rng(42)
    centers = real_embeddings(conn)
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute("CREATE TABLE items (bot INT NOT NULL, id INT NOT NULL, embedding vector(384) NOT NULL, "
                 "PRIMARY KEY (bot, id))")
    start = time.perf_counter()
    with conn.cursor().copy("COPY items (bot, id, embedding) FROM STDIN") as copy:
        for lo in range(0, n_chunks, 5000):
            n = min(5000, n_chunks - lo)
            v = centers[rng.integers(0, len(centers), n)] + rng.normal(0, NOISE, (n, centers.shape[1]))
            v /= np.linalg.norm(v, axis=1, keepdims=True)
            bots = rng.integers(0, n_bots, n)
            for i in range(n):
                copy.write_row((int(bots[i]), lo + i, "[" + ",".join(f"{x:.5f}" for x in v[i]) + "]"))
    load_s = time.perf_counter() - start
    conn.execute("SET maintenance_work_mem = '512MB'")
    start = time.perf_counter()
    conn.execute("CREATE INDEX items_hnsw ON items USING hnsw (embedding vector_cosine_ops)")
    build_s = time.perf_counter() - start
    conn.execute("ANALYZE items")
    sizes = conn.execute("SELECT pg_relation_size('items'), pg_relation_size('items_hnsw')").fetchone()
    print(f"Loaded {n_chunks} synthetic chunks in {load_s:.0f} s; HNSW index built in {build_s:.0f} s "
          f"({sizes[1] / 1e6:.0f} MB index for {sizes[0] / 1e6:.0f} MB of rows)")
    return {"load_s": round(load_s, 1), "index_build_s": round(build_s, 1),
            "table_mb": round(sizes[0] / 1e6), "index_mb": round(sizes[1] / 1e6)}


def run(conn, sql, params, settings: dict):
    """Run one search with planner settings in place; return (ids, median seconds, plan uses HNSW?)."""
    times, ids = [], []
    with conn.transaction():
        for name, value in settings.items():
            conn.execute(f"SET LOCAL {name} = {value}")
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql, params).fetchall())
        for _ in range(ROUNDS):
            start = time.perf_counter()
            ids = [r[0] for r in conn.execute(sql, params).fetchall()]
            times.append(time.perf_counter() - start)
    return ids, statistics.median(times), "items_hnsw" in plan


def recall(got, truth):
    return len(set(got) & set(truth)) / len(truth)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunks", type=int, default=50000)
    parser.add_argument("--bots", type=int, default=50)
    parser.add_argument("--reuse", action="store_true", help="skip building if the table exists")
    args = parser.parse_args()

    get_pool()  # makes sure the vector extension and public.chunks exist
    url = make_conninfo(DATABASE_URL, options=f"-c search_path={SCHEMA},public")
    # prepare_threshold=None: psycopg would otherwise prepare a query after 5 runs, and
    # Postgres would keep reusing that plan after the planner settings change.
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        version = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
        exists = conn.execute("SELECT to_regclass('items') IS NOT NULL").fetchone()[0]
        build_info = {} if (args.reuse and exists) else build(conn, args.chunks, args.bots)
        n_chunks = conn.execute("SELECT count(*) FROM items").fetchone()[0]

        embedder = get_embeddings()
        qs = [to_vector(embedder.embed_query(q)) for q in questions()]
        whole = "SELECT id FROM items ORDER BY embedding <=> %s::vector LIMIT 10"
        one_bot = "SELECT id FROM items WHERE bot = %s ORDER BY embedding <=> %s::vector LIMIT 10"
        exact = {"enable_indexscan": "off", "enable_bitmapscan": "off"}  # read every row: the truth
        no_hnsw = {"enable_indexscan": "off"}  # bitmap scan on the primary key, then compare each row
        use_hnsw = {"enable_seqscan": "off", "enable_bitmapscan": "off", "enable_sort": "off"}

        rows = []

        def record(name, recalls, times, used_hnsw, ef=None, returned=None):
            row = {"search": name, "ef_search": ef, "recall_at_10": round(statistics.mean(recalls), 3),
                   "p50_ms": round(np.percentile(times, 50) * 1000, 2),
                   "p95_ms": round(np.percentile(times, 95) * 1000, 2), "plan_uses_hnsw": used_hnsw}
            if returned is not None:
                row["avg_rows_returned"] = round(statistics.mean(returned), 1)
            rows.append(row)
            print(json.dumps(row))

        # Whole table
        truths, times = [], []
        for q in qs:
            ids, t, _ = run(conn, whole, (q,), exact)
            truths.append(ids)
            times.append(t)
        record("whole table: exact", [1.0] * len(qs), times, False)
        for ef in EF_VALUES:
            recs, times, used = [], [], set()
            for q, truth in zip(qs, truths):
                ids, t, u = run(conn, whole, (q,), {**use_hnsw, "hnsw.ef_search": ef})
                recs.append(recall(ids, truth))
                times.append(t)
                used.add(u)
            record("whole table: hnsw", recs, times, all(used), ef)

        # One bot (each question asked of a different bot)
        bot_of = [i % args.bots for i in range(len(qs))]
        truths, times = [], []
        for q, bot in zip(qs, bot_of):
            ids, t, _ = run(conn, one_bot, (bot, q), no_hnsw)
            truths.append(ids)
            times.append(t)
        record("one bot: exact-filtered", [1.0] * len(qs), times, False)
        variants = [("one bot: hnsw-filtered", {})]
        if tuple(int(x) for x in version.split(".")[:2]) >= (0, 8):
            variants.append(("one bot: hnsw-iterative", {"hnsw.iterative_scan": "strict_order"}))
        for name, extra in variants:
            for ef in (40, 160):
                recs, times, returned, used = [], [], [], set()
                for q, bot, truth in zip(qs, bot_of, truths):
                    ids, t, u = run(conn, one_bot, (bot, q), {**use_hnsw, **extra, "hnsw.ef_search": ef})
                    recs.append(recall(ids, truth))
                    times.append(t)
                    returned.append(len(ids))
                    used.add(u)
                record(name, recs, times, all(used), ef, returned)
        # What Postgres picks on its own for the one-bot query
        ids, t, used = run(conn, one_bot, (bot_of[0], qs[0]), {})
        print(f"Left to itself, the planner {'uses HNSW' if used else 'filters by bot and compares each row'} "
              f"for the one-bot query.")

    summary = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": "hnsw bench (synthetic)",
               "pgvector": version, "chunks": n_chunks, "bots": args.bots, "questions": len(qs),
               **build_info, "results": rows}
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    main()
