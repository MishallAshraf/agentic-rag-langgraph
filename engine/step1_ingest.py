"""Step 1a - Ingestion: PDF/DOCX -> documents -> chunks -> embeddings -> vector store.

Run once (and again whenever the documents or chunking settings change):

    python -m engine.step1_ingest
"""

from pathlib import Path

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

from engine.vectorstore import DOCUMENTS_DIR, ROOT, get_vectorstore

CHUNKS_DIR = ROOT / "chunks"

# A chunk should be big enough to hold a complete idea, but small enough that
# its embedding is about ONE topic. The overlap stops a sentence that straddles
# a boundary from being cut in half in both chunks.
CHUNK_SIZE = 1000  # characters
CHUNK_OVERLAP = 150

# File extension -> LangChain loader. Add a line here to support a new type.
LOADERS = {
    ".pdf": PyPDFLoader,
    ".docx": Docx2txtLoader,
}


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


def save_chunks(chunks, chunks_dir=CHUNKS_DIR):
    """Write each chunk to its own text file so the split can be inspected."""
    chunks_dir.mkdir(exist_ok=True)
    for old in chunks_dir.glob("chunk_*.txt"):
        old.unlink()
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.metadata
        section = f"section: {meta['section']}\n" if "section" in meta else ""
        header = (
            f"chunk: {i} of {len(chunks)}\n"
            f"source: {Path(meta['source']).name}\n"
            f"page: {format_page(meta)}\n"
            f"{section}"
            f"start_index: {meta['start_index']}\n"
            f"length: {len(chunk.page_content)} characters\n"
            f"{'-' * 60}\n"
        )
        path = chunks_dir / f"chunk_{i:03d}.txt"
        path.write_text(header + chunk.page_content, encoding="utf-8")


def main():
    pages = load_documents()
    chunks = split_documents(pages)
    print(f"Split into {len(chunks)} chunks")
    save_chunks(chunks)
    print(f"Saved chunks to {CHUNKS_DIR}")

    # EMBED + STORE: the store calls the embedding model on every chunk and saves
    # (vector, text, metadata) as rows in Postgres. Reset first so re-running this script
    # replaces the collection instead of adding duplicates.
    store = get_vectorstore()
    store.reset_collection()
    store.add_documents(chunks)
    print(f"Stored {store.count()} chunks in the vector store")

    sample = chunks[len(chunks) // 2]
    print("\n--- sample chunk ---")
    print("metadata:", sample.metadata)
    print(sample.page_content[:400])


if __name__ == "__main__":
    main()
