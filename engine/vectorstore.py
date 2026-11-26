"""Shared access to the vector store: document chunks in Postgres with pgvector.

Both ingestion (writing chunks) and retrieval (searching chunks) must use the
SAME embedding model, otherwise the query vector and the stored vectors live in
different spaces and similarity is meaningless. Keeping it in one place
guarantees that.

Until Lampus Phase 1, Step 5 the chunks lived in Chroma, a folder on one
machine. Now they live in the same Postgres as users and conversations
(engine/store/vectors.sql), so every API copy searches the same documents, and
there is one database to back up. Each bot's chunks are rows tagged with its
collection name; every search filters on it.

Since Lampus Phase 1, Step 9 searches can go to a read replica (DATABASE_READ_URL):
a second Postgres that receives every change from the primary a few milliseconds
later and serves reads, so the primary is left for writes and for reads that
must see their own writes. Searches can wait those milliseconds; a student's
documents don't change between two messages. If the replica can't be reached,
searches go to the primary for the next 30 s and then try it again. Writes,
and the reads ingestion uses to compare (get, embeddings_of, count), always use
the primary.

    store = get_vectorstore("bot_customer_handbook")
    store.reset_collection()
    store.add_documents(chunks, ids=[...])
    store.similarity_search("When does the cafe open?", k=4, filter={"section": "body"})
"""

import logging
import os
import threading
import time
import uuid
from pathlib import Path

import psycopg

from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from langchain_core.documents import Document
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from engine.db import DATABASE_URL, POOL_MAX

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = ROOT / "documents"
COLLECTION_NAME = "documents"
SCHEMA_FILE = Path(__file__).parent / "store" / "vectors.sql"

# Small (~130 MB) embedding model that runs locally on CPU via ONNX.
# It turns a piece of text into a 384-dimensional vector.
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# Empty: searches use the primary too. Leave it unset for build workers: they search
# chunks they have just written, which the replica may not have yet.
DATABASE_READ_URL = os.getenv("DATABASE_READ_URL", "")
REPLICA_WAIT_S = 2      # how long a search waits for a replica connection before using the primary
REPLICA_RETRY_S = 30    # after a failure, how long to use the primary before trying the replica again

log = logging.getLogger("vectorstore")
_lock = threading.Lock()
_pools: dict[str, ConnectionPool] = {}
_read_pools: dict[str, ConnectionPool] = {}
_replica_down_until = 0.0
_embeddings: FastEmbedEmbeddings | None = None


def get_embeddings() -> FastEmbedEmbeddings:
    global _embeddings
    with _lock:  # one copy of the model per process, however many bots
        if _embeddings is None:
            _embeddings = FastEmbedEmbeddings(model_name=EMBEDDING_MODEL)
    return _embeddings


def get_pool(url: str = DATABASE_URL) -> ConnectionPool:
    """One pool per process for searches. Retrieval is synchronous (it runs in the
    agents' tool threads), so this is psycopg's thread-safe pool, not the async one."""
    with _lock:
        if url not in _pools:
            pool = ConnectionPool(url, min_size=1, max_size=POOL_MAX, open=True,
                                  kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None})
            with pool.connection() as conn, conn.transaction():
                # Same lock as engine/db.py, so two processes never create the tables at once.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('lampus.schema'))")
                conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
            _pools[url] = pool
    return _pools[url]


def get_read_pool(url: str) -> ConnectionPool:
    """A pool on the read replica. No schema set-up: a replica refuses writes, and
    it receives the tables from the primary."""
    with _lock:
        if url not in _read_pools:
            _read_pools[url] = ConnectionPool(url, min_size=1, max_size=POOL_MAX, open=True,
                                              kwargs={"row_factory": dict_row, "autocommit": True,
                                                      "prepare_threshold": None})
    return _read_pools[url]


def to_vector(values) -> str:
    """pgvector's text format: '[0.1,0.2,...]'."""
    return "[" + ",".join(str(float(v)) for v in values) + "]"


class VectorStore:
    """One bot's collection of chunks."""

    def __init__(self, collection: str, url: str = DATABASE_URL, read_url: str | None = None):
        self.collection = collection
        self.pool = get_pool(url)
        read_url = DATABASE_READ_URL if read_url is None else read_url
        self.read_pool = get_read_pool(read_url) if read_url else None

    @property
    def embeddings(self) -> FastEmbedEmbeddings:
        return get_embeddings()  # loaded on first use: searching by a ready vector never needs it

    def reset_collection(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE collection = %s", (self.collection,))
            # Forget the files too (lineage, Step 6), so the next ingest redoes every file.
            conn.execute("DELETE FROM source_files WHERE collection = %s", (self.collection,))

    def add_documents(self, docs: list[Document], ids: list[str] | None = None) -> list[str]:
        ids = ids or [str(uuid.uuid4()) for _ in docs]
        vectors = self.embeddings.embed_documents([d.page_content for d in docs])
        self.add_embedded(ids, [d.page_content for d in docs], [d.metadata for d in docs], vectors)
        return ids

    def add_embedded(self, ids, texts, metadatas, vectors) -> None:
        """Store chunks whose embeddings are already computed (used by the Chroma migration)."""
        rows = [(self.collection, i, t, Jsonb(m or {}), to_vector(v))
                for i, t, m, v in zip(ids, texts, metadatas, vectors)]
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (collection, id, content, metadata, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector) ON CONFLICT (collection, id) DO UPDATE SET "
                "content = EXCLUDED.content, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding",
                rows)

    def similarity_search(self, query: str, k: int = 4, filter: dict | None = None) -> list[Document]:
        return self.similarity_search_by_vector(self.embeddings.embed_query(query), k, filter)

    def similarity_search_by_vector(self, vector, k: int = 4, filter: dict | None = None) -> list[Document]:
        # <=> is cosine distance. The WHERE keeps the search inside this bot; its
        # chunks are found by the primary key and compared one by one, which is exact
        # (why there is no HNSW index: engine/store/vectors.sql).
        rows = self._search("SELECT id, content, metadata FROM chunks WHERE collection = %s AND metadata @> %s "
                            "ORDER BY embedding <=> %s::vector LIMIT %s",
                            (self.collection, Jsonb(filter or {}), to_vector(vector), k))
        return [Document(page_content=r["content"], metadata=r["metadata"], id=r["id"]) for r in rows]

    def _search(self, sql: str, params: tuple) -> list[dict]:
        """Run a search on the replica if there is one and it answers, else on the primary."""
        global _replica_down_until
        if self.read_pool is not None and time.monotonic() >= _replica_down_until:
            try:
                with self.read_pool.connection(timeout=REPLICA_WAIT_S) as conn:
                    return conn.execute(sql, params).fetchall()
            except psycopg.OperationalError as e:  # includes the pool's timeout
                _replica_down_until = time.monotonic() + REPLICA_RETRY_S
                log.warning("read replica unavailable, searching the primary for %d s: %s", REPLICA_RETRY_S, e)
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def get(self, where: dict | None = None) -> list[Document]:
        """Every chunk of this collection (optionally matching a metadata filter), in id order."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, content, metadata FROM chunks WHERE collection = %s AND metadata @> %s ORDER BY id",
                (self.collection, Jsonb(where or {}))).fetchall()
        return [Document(page_content=r["content"], metadata=r["metadata"], id=r["id"]) for r in rows]

    def embeddings_of(self, where: dict | None = None) -> tuple[list[str], list[list[float]]]:
        """Chunk ids and stored vectors, for checks against an exact search."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, embedding::text AS e FROM chunks WHERE collection = %s AND metadata @> %s ORDER BY id",
                (self.collection, Jsonb(where or {}))).fetchall()
        return [r["id"] for r in rows], [[float(x) for x in r["e"].strip("[]").split(",")] for r in rows]

    def count(self) -> int:
        with self.pool.connection() as conn:
            return conn.execute("SELECT count(*) AS n FROM chunks WHERE collection = %s",
                                (self.collection,)).fetchone()["n"]


def get_vectorstore(collection_name: str = COLLECTION_NAME) -> VectorStore:
    return VectorStore(collection_name)
