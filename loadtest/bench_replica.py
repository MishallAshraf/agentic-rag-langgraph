"""Where do searches run, and how far behind is the read replica? (Lampus Phase 1, Step 9)

    python loadtest/bench_replica.py --label "..."   # while or after a load test

Two numbers:

  1. Searches on the primary: Postgres counts, per server, every time a table is
     read (pg_stat_user_tables). The script reads the counts for the chunks table
     on the primary and on the replica, waits while a load test runs
     (--watch seconds), and reads them again. Without a replica every search is
     on the primary; with one, the primary should see (almost) none.
  2. Replication lag: write on the primary, then time how long until the replica
     has replayed that write (compares WAL positions: the primary's log of
     changes, and how far the replica has applied it). Repeated --samples times.

Connection settings: DATABASE_URL (the primary) and DATABASE_READ_URL (the
replica; leave unset to measure the primary alone).
"""

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

RESULTS = Path(__file__).parent / "results.jsonl"
SCANS = ("SELECT coalesce(sum(seq_scan + coalesce(idx_scan, 0)), 0) AS n FROM pg_stat_user_tables "
         "WHERE relname = 'chunks' AND schemaname = 'loadtest'")


def scans(url: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        # Statistics are cached per transaction; make sure this read is fresh.
        conn.execute("SELECT pg_stat_clear_snapshot()")
        return int(conn.execute(SCANS).fetchone()[0])


def lag_ms(primary: str, replica: str, samples: int) -> list[float]:
    times = []
    with psycopg.connect(primary, autocommit=True) as p, psycopg.connect(replica, autocommit=True) as r:
        p.execute("CREATE TABLE IF NOT EXISTS loadtest.replica_probe (n int)")
        for i in range(samples):
            p.execute("INSERT INTO loadtest.replica_probe VALUES (%s)", (i,))
            lsn = p.execute("SELECT pg_current_wal_lsn()").fetchone()[0]
            start = time.perf_counter()
            while not r.execute("SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn", (lsn,)).fetchone()[0]:
                time.sleep(0.0005)
            times.append((time.perf_counter() - start) * 1000)
            time.sleep(0.05)
        p.execute("DROP TABLE loadtest.replica_probe")
    return times


def main(label: str, watch: float, samples: int):
    primary, replica = os.environ["DATABASE_URL"], os.environ.get("DATABASE_READ_URL", "")
    before = (scans(primary), scans(replica) if replica else 0)
    time.sleep(watch)
    after = (scans(primary), scans(replica) if replica else 0)
    on_primary, on_replica = after[0] - before[0], after[1] - before[1]
    result = {"chunk_reads_on_primary": on_primary, "chunk_reads_on_replica": on_replica,
              "share_on_primary": round(on_primary / (on_primary + on_replica), 3) if on_primary + on_replica else None}
    if replica:
        lags = sorted(lag_ms(primary, replica, samples))
        result.update({"lag_ms_p50": round(statistics.median(lags), 2),
                       "lag_ms_p95": round(lags[int(0.95 * (len(lags) - 1))], 2),
                       "lag_ms_max": round(lags[-1], 2), "lag_samples": samples})
    print(json.dumps(result, indent=2))
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "label": label, **result}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="replica")
    parser.add_argument("--watch", type=float, default=60, help="seconds to count reads while a load test runs")
    parser.add_argument("--samples", type=int, default=200)
    args = parser.parse_args()
    main(args.label, args.watch, args.samples)
