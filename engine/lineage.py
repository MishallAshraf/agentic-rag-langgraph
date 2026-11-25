"""Lampus Phase 1, Step 6 - Data lineage and incremental ingestion (course topic 22).

Lineage answers "where did this chunk come from?": the file (and its exact
content, by SHA-256 hash), the ingestion run that made the chunk, and that
run's settings (embedding model, chunk size, loader) and code version. A
teacher who asks why the tutor said something can be shown the source, and
after a bad ingest we can see which chunks it touched.

The same records make ingestion incremental. A file whose hash and settings
match the last run is skipped; an edited file is re-chunked, but a chunk whose
text didn't change keeps its stored vector instead of being embedded again.

    python -m engine.lineage runs [collection]          # recent ingestion runs
    python -m engine.lineage chunk <collection> <id>    # where one chunk came from

Ingestion calls sync() (engine/ingest.py, engine/step6_ingest.py).
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from langchain_core.documents import Document
from psycopg.types.json import Jsonb

from engine.step1_ingest import CHUNK_OVERLAP, CHUNK_SIZE
from engine.vectorstore import EMBEDDING_MODEL, ROOT, get_pool, get_vectorstore, to_vector

# Bump when loading or splitting changes in a way that changes chunks, so every
# file is re-chunked on the next run even though the files themselves didn't change.
PIPELINE_VERSION = 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_commit() -> str | None:
    """The code version that ran the ingest, e.g. 'f1b79e9' or 'f1b79e9+changes'."""
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("+changes" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None  # not a git checkout (e.g. a container image without .git)


def run_settings(loader: str) -> dict:
    return {"embedding_model": EMBEDDING_MODEL, "chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP,
            "loader": loader, "pipeline_version": PIPELINE_VERSION}


def settings_hash(settings: dict) -> str:
    return sha256_bytes(json.dumps(settings, sort_keys=True).encode())[:16]


def sync(collection: str, folder: Path, files: list[Path], chunk_file: Callable[[Path], list[Document]],
         chunk_id: Callable[[Document], str], loader: str) -> dict:
    """Bring a collection in line with the files: add new ones, redo edited ones, drop removed ones.

    chunk_file(path) loads and splits one file; chunk_id(chunk) names a chunk.
    Each file's old chunks are replaced in one transaction, so a search never
    sees half of an old and half of a new version.
    """
    store = get_vectorstore(collection)
    settings = run_settings(loader)
    s_hash = settings_hash(settings)
    stats = {"files_seen": len(files), "files_changed": 0, "files_removed": 0,
             "chunks_embedded": 0, "chunks_reused": 0}

    with store.pool.connection() as conn:
        # One ingest per collection at a time (e.g. two admins pressing "rebuild").
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"lampus.ingest.{collection}",))
        try:
            run_id = conn.execute(
                "INSERT INTO ingest_runs (collection, folder, git_commit, settings) VALUES (%s, %s, %s, %s) "
                "RETURNING id", (collection, folder.relative_to(ROOT).as_posix(), git_commit(), Jsonb(settings))
            ).fetchone()["id"]
            # Chunks from before lineage existed (e.g. copied from Chroma) have no source: start them afresh.
            conn.execute("DELETE FROM chunks WHERE collection = %s AND source_path IS NULL", (collection,))
            known = {r["path"]: r for r in conn.execute(
                "SELECT path, sha256, settings_hash FROM source_files WHERE collection = %s", (collection,))}

            current = {f.relative_to(folder).as_posix(): f for f in files}
            for path, file in current.items():
                data = file.read_bytes()
                file_hash = sha256_bytes(data)
                before = known.get(path)
                if before and before["sha256"] == file_hash and before["settings_hash"] == s_hash:
                    continue  # unchanged since the last run
                stats["files_changed"] += 1
                chunks = chunk_file(file)
                text_hashes = [sha256_bytes(c.page_content.encode()) for c in chunks]

                # Same settings as before: a chunk whose text is unchanged keeps its vector.
                old_vectors = {}
                if before and before["settings_hash"] == s_hash:
                    old_vectors = {r["content_sha256"]: r["e"] for r in conn.execute(
                        "SELECT content_sha256, embedding::text AS e FROM chunks "
                        "WHERE collection = %s AND source_path = %s", (collection, path))}
                missing = [i for i, h in enumerate(text_hashes) if h not in old_vectors]
                new_vectors = store.embeddings.embed_documents([chunks[i].page_content for i in missing]) \
                    if missing else []
                vectors = {i: to_vector(v) for i, v in zip(missing, new_vectors)}
                stats["chunks_embedded"] += len(missing)
                stats["chunks_reused"] += len(chunks) - len(missing)

                rows = [(collection, chunk_id(c), c.page_content, Jsonb(c.metadata),
                         vectors.get(i) or old_vectors[text_hashes[i]], path, text_hashes[i], run_id)
                        for i, c in enumerate(chunks)]
                with conn.transaction():
                    conn.execute("DELETE FROM chunks WHERE collection = %s AND source_path = %s", (collection, path))
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO chunks (collection, id, content, metadata, embedding, source_path, "
                            "content_sha256, ingest_run) VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s) "
                            "ON CONFLICT (collection, id) DO UPDATE SET content = EXCLUDED.content, "
                            "metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding, "
                            "source_path = EXCLUDED.source_path, content_sha256 = EXCLUDED.content_sha256, "
                            "ingest_run = EXCLUDED.ingest_run", rows)
                    conn.execute(
                        "INSERT INTO source_files (collection, path, sha256, size_bytes, settings_hash, ingest_run) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (collection, path) DO UPDATE SET "
                        "sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes, "
                        "settings_hash = EXCLUDED.settings_hash, ingest_run = EXCLUDED.ingest_run",
                        (collection, path, file_hash, len(data), s_hash, run_id))

            for path in known.keys() - current.keys():  # files deleted from the folder
                with conn.transaction():
                    conn.execute("DELETE FROM chunks WHERE collection = %s AND source_path = %s", (collection, path))
                    conn.execute("DELETE FROM source_files WHERE collection = %s AND path = %s", (collection, path))
                stats["files_removed"] += 1

            conn.execute(
                "UPDATE ingest_runs SET finished_at = now(), files_seen = %(files_seen)s, "
                "files_changed = %(files_changed)s, files_removed = %(files_removed)s, "
                "chunks_embedded = %(chunks_embedded)s, chunks_reused = %(chunks_reused)s WHERE id = %(id)s",
                {**stats, "id": run_id})
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"lampus.ingest.{collection}",))
    return {**stats, "run": run_id, "chunks": store.count()}


# ------------------------------------------------------------------ lookups

def chunk_lineage(collection: str, chunk_id: str) -> dict | None:
    """Everything known about where one chunk came from."""
    store = get_vectorstore(collection)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT c.id, c.source_path, c.content_sha256, c.content, r.id AS run, r.folder, r.started_at, "
            "r.git_commit, r.settings, f.sha256 AS file_sha256, f.size_bytes "
            "FROM chunks c LEFT JOIN ingest_runs r ON r.id = c.ingest_run "
            "LEFT JOIN source_files f ON f.collection = c.collection AND f.path = c.source_path "
            "WHERE c.collection = %s AND c.id = %s", (collection, chunk_id)).fetchone()
    if row is None:
        return None
    if row["source_path"] and row["folder"]:
        file = ROOT / row["folder"] / row["source_path"]
        row["file_now"] = ("missing" if not file.exists() else
                           "unchanged" if sha256_bytes(file.read_bytes()) == row["file_sha256"] else
                           "edited since this run")
    return row


def recent_runs(collection: str | None = None, limit: int = 10) -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, collection, started_at, finished_at, git_commit, files_seen, files_changed, "
            "files_removed, chunks_embedded, chunks_reused FROM ingest_runs "
            "WHERE %(c)s::text IS NULL OR collection = %(c)s ORDER BY id DESC LIMIT %(n)s",
            {"c": collection, "n": limit}).fetchall()


def main():
    args = sys.argv[1:]
    if args[:1] == ["runs"] and len(args) <= 2:
        for r in recent_runs(args[1] if len(args) == 2 else None):
            status = "ok" if r["finished_at"] else "unfinished"
            print(f"run {r['id']:>4}  {r['started_at']:%Y-%m-%d %H:%M}  {r['collection']:<24} {status:<10} "
                  f"code {r['git_commit'] or '?':<16} files {r['files_seen']} seen / {r['files_changed']} changed / "
                  f"{r['files_removed']} removed, chunks {r['chunks_embedded']} embedded / {r['chunks_reused']} reused")
    elif args[:1] == ["chunk"] and len(args) == 3:
        info = chunk_lineage(args[1], args[2])
        if info is None:
            raise SystemExit(f"No chunk {args[2]!r} in {args[1]!r}")
        if not info["run"]:
            print(f"{info['id']}: no lineage (stored before Step 6; re-ingest the bot to record it)")
            return
        print(f"chunk     {info['id']}\n"
              f"text      {' '.join(info['content'].split())[:100]}...\n"
              f"file      {info['folder']}/{info['source_path']} ({info['size_bytes']} bytes, "
              f"sha256 {info['file_sha256'][:12]}), {info['file_now']}\n"
              f"run       {info['run']} at {info['started_at']:%Y-%m-%d %H:%M} with code {info['git_commit']}\n"
              f"settings  {json.dumps(info['settings'])}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
