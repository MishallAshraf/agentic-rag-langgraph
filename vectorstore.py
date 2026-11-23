"""Shared access to the vector store.

Both ingestion (writing chunks) and retrieval (searching chunks) must use the
SAME embedding model, otherwise the query vector and the stored vectors live in
different spaces and similarity is meaningless. Keeping it in one place
guarantees that.
"""

from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

ROOT = Path(__file__).parent
DOCUMENTS_DIR = ROOT / "documents"
CHROMA_DIR = ROOT / "chroma_db"
COLLECTION_NAME = "documents"

# Small (~130 MB) embedding model that runs locally on CPU via ONNX.
# It turns a piece of text into a 384-dimensional vector.
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def get_embeddings() -> FastEmbedEmbeddings:
    return FastEmbedEmbeddings(model_name=EMBEDDING_MODEL)


def get_vectorstore(collection_name: str = COLLECTION_NAME) -> Chroma:
    """Open a persistent Chroma collection (created on first use)."""
    return Chroma(
        collection_name=collection_name,
        embedding_function=get_embeddings(),
        persist_directory=str(CHROMA_DIR),
    )
