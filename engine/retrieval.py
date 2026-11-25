"""Step 6b - Better retrieval: hybrid search plus reranking.

Four ways to find chunks, each building on the last:

  vector   meaning-based: embed the query, find the nearest chunk vectors.
           Good at paraphrases ("why do therapies give similar results?").
  keyword  BM25: scores chunks by shared words, weighting rare words higher.
           Good at exact terms and names ("Dodo bird", "Lambert", "89").
  hybrid   runs both and merges the two rankings with Reciprocal Rank Fusion.
  rerank   takes the hybrid top 10 and re-scores each (query, chunk) pair with
           a cross-encoder, a small model that reads both texts together.

Why rerank at all? The embedding model encodes the query and each chunk
SEPARATELY, so it cannot compare them word by word. A cross-encoder reads them
together, which is far more accurate but too slow to run on every chunk -
hence: cheap search for 10 candidates, expensive scoring for those 10 only.
"""

import re
import threading

from fastembed.rerank.cross_encoder import TextCrossEncoder
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from engine.vectorstore import get_vectorstore

CLEAN_COLLECTION = "documents_clean"
# How many results each method contributes before fusion or reranking. Measured
# with step6_compare.py: 10 gave the same quality as 20 at half the rerank time.
CANDIDATES = 10
RRF_K = 60  # standard RRF constant: dampens the gap between rank 1 and rank 2
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"  # ~80 MB, CPU-friendly
BODY_ONLY = {"section": "body"}  # metadata filter: skip reference lists
DEFAULT_MODE = "rerank"  # what search() uses unless told otherwise; evals can switch it

STOPWORDS = set(
    "a an and are as at be by can do does for from how in is it of on or that the "
    "this to was what when which who why will with you your".split()
)


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


# Searches can run in parallel threads (the agent's tools, the eval runner, later
# the API). Without the lock, two threads can both see "not created yet" and
# open the Chroma store at the same time, which Chroma rejects.
_lock = threading.RLock()
_reranker = None


def get_reranker() -> TextCrossEncoder:
    """The cross-encoder is the same for every collection, so load it once for all."""
    global _reranker
    with _lock:  # loading the model twice at once, from two threads, fails
        if _reranker is None:
            _reranker = TextCrossEncoder(RERANK_MODEL)
    return _reranker


class Retriever:
    def __init__(self, collection: str = CLEAN_COLLECTION):
        self.store = get_vectorstore(collection)
        # BM25 keeps its own index in memory, built from the same chunks.
        data = self.store.get(where=BODY_ONLY, include=["documents", "metadatas"])
        if not data["ids"]:
            raise SystemExit(f"Collection '{collection}' is empty. Ingest its documents first "
                             "(python -m engine.step6_ingest, or python -m engine.ingest bots/<bot>.yaml).")
        self.docs = [
            Document(page_content=text, metadata=meta, id=doc_id)
            for doc_id, text, meta in zip(data["ids"], data["documents"], data["metadatas"])
        ]
        self.bm25 = BM25Okapi([tokenize(d.page_content) for d in self.docs])

    def vector(self, query: str, k: int) -> list[Document]:
        return self.store.similarity_search(query, k=k, filter=BODY_ONLY)

    def keyword(self, query: str, k: int) -> list[Document]:
        scores = self.bm25.get_scores(tokenize(query))
        best = sorted(range(len(self.docs)), key=lambda i: scores[i], reverse=True)[:k]
        return [self.docs[i] for i in best if scores[i] > 0]

    def hybrid(self, query: str, k: int) -> list[Document]:
        # Reciprocal Rank Fusion: each method votes 1/(RRF_K + rank) for each
        # chunk it returned. Uses only ranks, so the two methods' very
        # different score scales never need to be compared.
        votes, by_id = {}, {}
        for results in (self.vector(query, CANDIDATES), self.keyword(query, CANDIDATES)):
            for rank, doc in enumerate(results, start=1):
                votes[doc.id] = votes.get(doc.id, 0) + 1 / (RRF_K + rank)
                by_id[doc.id] = doc
        return [by_id[i] for i in sorted(votes, key=votes.get, reverse=True)[:k]]

    def rerank(self, query: str, k: int) -> list[Document]:
        candidates = self.hybrid(query, CANDIDATES)
        scores = list(get_reranker().rerank(query, [d.page_content for d in candidates]))
        best = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)[:k]
        return [candidates[i] for i in best]

    def search(self, query: str, k: int = 4, mode: str | None = None) -> list[Document]:
        return {"vector": self.vector, "keyword": self.keyword,
                "hybrid": self.hybrid, "rerank": self.rerank}[mode or DEFAULT_MODE](query, k)


_retrievers: dict[str, Retriever] = {}


def get_retriever(collection: str = CLEAN_COLLECTION) -> Retriever:
    """One Retriever per collection (Step 10: one collection per bot), built once."""
    with _lock:
        if collection not in _retrievers:
            _retrievers[collection] = Retriever(collection)
    return _retrievers[collection]


def forget_retriever(collection: str) -> None:
    """Drop a cached Retriever, e.g. after its collection was re-ingested."""
    with _lock:
        _retrievers.pop(collection, None)
