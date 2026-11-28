"""Ingest the tutor's textbook into the vector store.

    python -m lampus.ingest_textbook            # web text + printed pages, cut by the book's structure
    python -m lampus.ingest_textbook --fixed    # the engine's PDF loader and 1,000-character chunks (the "before")

Uses the Step 6 lineage sync (engine/lineage.py): run it again and only edited
files are re-chunked, only changed chunks embedded. Download the book first:
python -m lampus.textbooks.fetch
"""

import argparse
import time
from functools import lru_cache
from pathlib import Path

from langchain_core.documents import Document

from engine.ingest import chunk_id as engine_chunk_id
from engine.lineage import sync
from engine.step1_ingest import split_documents
from engine.step6_ingest import load_pdf
from lampus.textbook import FOLDER, PDF, WEB, PdfPages, load_page

COLLECTION = "prealgebra"
FIXED_COLLECTION = "prealgebra_fixed"
CHAPTERS = (4, 5, 6)  # the chapters fetched; see lampus/textbooks/fetch.py


@lru_cache(maxsize=1)
def pdf_pages() -> PdfPages:
    return PdfPages(PDF)


def sections_chunks(file: Path) -> list[Document]:
    docs, _ = load_page(file, pdf_pages())
    for i, doc in enumerate(docs):
        doc.metadata["n"] = i
    return docs


def sections_id(chunk: Document) -> str:
    return f"prealgebra-2e:{chunk.metadata['slug']}:{chunk.metadata['n']:03d}"


def fixed_ranges() -> list[tuple[int, int]]:
    """The same chapters, and their part of the answer key, as 0-based PDF pages."""
    pages = pdf_pages()
    ranges = [pages.range_of(f"Chapter {c} ", level=1) for c in CHAPTERS]
    ranges += [pages.range_of(f"Chapter {c}", within="Answer Key") for c in CHAPTERS]
    return ranges


def fixed_chunks(file: Path) -> list[Document]:
    ranges = fixed_ranges()
    docs = [d for d in load_pdf(file) if any(lo <= d.metadata["page"] < hi for lo, hi in ranges)]
    chunks = split_documents(docs)
    for chunk in chunks:
        chunk.metadata["source"] = file.name
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixed", action="store_true", help="the engine's PDF loader and fixed-size chunks")
    args = parser.parse_args()
    if not PDF.exists():
        raise SystemExit("The textbook isn't downloaded. Run: python -m lampus.textbooks.fetch")
    started = time.perf_counter()
    if args.fixed:
        stats = sync(FIXED_COLLECTION, FOLDER, [PDF], fixed_chunks, engine_chunk_id, loader="lampus.ingest_textbook --fixed")
    else:
        files = sorted(WEB.glob("*.html"))

        def chunk_file(file: Path) -> list[Document]:
            docs = sections_chunks(file)
            for doc in docs:
                doc.metadata["slug"] = file.stem
            return docs

        stats = sync(COLLECTION, FOLDER, files, chunk_file, sections_id, loader="lampus.ingest_textbook")
    print(f"{stats['chunks']} chunks in '{FIXED_COLLECTION if args.fixed else COLLECTION}' "
          f"({stats['files_changed']} files changed, {stats['chunks_embedded']} chunks embedded, "
          f"{stats['chunks_reused']} reused) in {time.perf_counter() - started:.0f} s")


if __name__ == "__main__":
    main()
