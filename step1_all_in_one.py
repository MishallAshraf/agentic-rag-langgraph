"""Step 1 in a single file (PDF and DOCX supported): ingestion and retrieval together.

Same logic as vectorstore.py + step1_ingest.py + step1_search.py, just in one
place so the whole flow can be read top to bottom.

    python step1_all_in_one.py ingest
    python step1_all_in_one.py search "What are the common factors of change?"
    python step1_all_in_one.py search          # interactive
"""

import sys
from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ---------------------------------------------------------------- settings

ROOT = Path(__file__).parent
DOCUMENTS_DIR = ROOT / "documents"
CHUNKS_DIR = ROOT / "chunks"
CHROMA_DIR = ROOT / "chroma_db"
COLLECTION_NAME = "documents"

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
CHUNK_SIZE = 1000  # characters
CHUNK_OVERLAP = 150
TOP_K = 4

# File extension -> LangChain loader. Add a line here to support a new type.
LOADERS = {
    ".pdf": PyPDFLoader,
    ".docx": Docx2txtLoader,
}

# ------------------------------------------------------------ vector store


def get_vectorstore() -> Chroma:
    """Open the persistent Chroma collection (created on first use).

    Used by BOTH ingestion and search, so they always share one embedding model.
    """
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=FastEmbedEmbeddings(model_name=EMBEDDING_MODEL),
        persist_directory=str(CHROMA_DIR),
    )


# --------------------------------------------------------------- ingestion


def load_documents():
    """LOAD: pick a loader by file extension; other file types are skipped.

    PDFs give one Document per page (metadata has "page"). A DOCX has no fixed
    pages, so it comes back as ONE Document for the whole file, without "page".
    """
    docs = []
    for file in sorted(DOCUMENTS_DIR.iterdir()):
        loader_class = LOADERS.get(file.suffix.lower())
        if loader_class is None:
            continue
        loaded = loader_class(str(file)).load()
        print(f"Loaded {len(loaded):>3} documents from {file.name}")
        docs.extend(loaded)
    return docs


def split_documents(pages):
    """SPLIT: tries paragraph breaks first, then line breaks, then spaces."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,
    )
    return splitter.split_documents(pages)


def format_page(metadata) -> str:
    """1-based page number, or "-" for formats without pages (DOCX)."""
    page = metadata.get("page")
    return "-" if page is None else str(page + 1)


def save_chunks(chunks):
    """Write each chunk to its own text file so the split can be inspected."""
    CHUNKS_DIR.mkdir(exist_ok=True)
    for old in CHUNKS_DIR.glob("chunk_*.txt"):
        old.unlink()
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.metadata
        header = (
            f"chunk: {i} of {len(chunks)}\n"
            f"source: {Path(meta['source']).name}\n"
            f"page: {format_page(meta)}\n"
            f"start_index: {meta['start_index']}\n"
            f"length: {len(chunk.page_content)} characters\n"
            f"{'-' * 60}\n"
        )
        path = CHUNKS_DIR / f"chunk_{i:03d}.txt"
        path.write_text(header + chunk.page_content, encoding="utf-8")


def ingest():
    """PDF -> pages -> chunks -> embeddings -> vector store."""
    pages = load_documents()
    chunks = split_documents(pages)
    print(f"Split into {len(chunks)} chunks")
    save_chunks(chunks)
    print(f"Saved chunks to {CHUNKS_DIR}")

    # EMBED + STORE. Reset first so a re-run replaces instead of duplicating.
    store = get_vectorstore()
    store.reset_collection()
    store.add_documents(chunks)
    print(f"Stored {store._collection.count()} chunks in the vector store")


# ------------------------------------------------------------------ search


def search(store, question: str):
    """Question -> embedding -> nearest chunks. Lower distance = closer."""
    results = store.similarity_search_with_score(question, k=TOP_K)
    print(f"\nQ: {question}")
    for rank, (doc, distance) in enumerate(results, start=1):
        page = format_page(doc.metadata)
        text = " ".join(doc.page_content.split())
        print(f"\n[{rank}] distance={distance:.3f}  page={page}")
        print(f"    {text[:300]}...")


def search_loop(question: str = ""):
    store = get_vectorstore()
    if store._collection.count() == 0:
        print("The vector store is empty. Run: python step1_all_in_one.py ingest")
        return
    if question:
        search(store, question)
        return
    while True:
        question = input("\nAsk (blank to quit): ").strip()
        if not question:
            break
        search(store, question)


# -------------------------------------------------------------------- main


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "ingest":
        ingest()
    elif command == "search":
        search_loop(" ".join(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
