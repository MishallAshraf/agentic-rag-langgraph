"""Step 6a - Cleaner ingestion into a second collection.

What changes compared with step1_ingest.py, and why (measured on our PDF):
  1. PyMuPDF instead of pypdf: words split by stray spaces ("c ounselling")
     dropped from about 198 to about 5.
  2. Page footers ("12 | COMMON AND SPECIFIC FACTORS OF CHANGE") are removed.
     They repeated the chapter title on every page and pulled reference-list
     chunks to the top of searches.
  3. Reference lists are tagged section="references" instead of "body", so
     search can filter them out with a metadata filter.
  4. Every chunk gets a stable id, so results from different search methods
     can be recognised as the same chunk and merged (step 6b).

The Step 1 collection is left untouched so both can be compared:

    python -m engine.step6_ingest
    python -m archive.steps.step6_compare
"""

import re
from pathlib import Path

import pymupdf
from langchain_community.document_loaders import Docx2txtLoader
from langchain_core.documents import Document

from engine.lineage import sync
from engine.step1_ingest import format_page, save_chunks, split_documents
from engine.vectorstore import DOCUMENTS_DIR, ROOT

CLEAN_COLLECTION = "documents_clean"
CHUNKS_DIR = ROOT / "chunks_clean"

# A footer line is a page number and a title separated by "|", either way round.
FOOTER = re.compile(r"^\s*(\d+\s*\|.*|.*\|\s*\d+)\s*$")
# A new chapter starts on a page whose first line is just "1." or "2." etc.
CHAPTER_START = re.compile(r"^\d+\.$")


def load_pdf(path: Path) -> list[Document]:
    """One or two Documents per page: the body text, and any reference list."""
    docs = []
    in_references = False
    for page_number, page in enumerate(pymupdf.open(path)):
        lines = [line for line in page.get_text().splitlines() if not FOOTER.match(line)]
        first = next((line.strip() for line in lines if line.strip()), "")
        if CHAPTER_START.match(first):
            in_references = False  # a new chapter ends the previous reference list

        body, references = [], []
        for line in lines:
            if line.strip().lower() == "references":
                in_references = True
            (references if in_references else body).append(line)

        for section, part in (("body", body), ("references", references)):
            text = "\n".join(part).strip()
            if text:
                metadata = {"source": str(path), "page": page_number, "section": section}
                docs.append(Document(page_content=text, metadata=metadata))
    return docs


def load_docx(path: Path) -> list[Document]:
    docs = Docx2txtLoader(str(path)).load()
    for doc in docs:
        doc.metadata["section"] = "body"
    return docs


LOADERS = {".pdf": load_pdf, ".docx": load_docx}


def chunk_id(chunk: Document) -> str:
    meta = chunk.metadata
    name = Path(meta["source"]).name
    return f"{name}:p{format_page(meta)}:{meta['section']}:{meta['start_index']}"


def chunk_file(file: Path) -> list[Document]:
    return split_documents(LOADERS[file.suffix.lower()](file))


def main():
    files = [f for f in sorted(DOCUMENTS_DIR.iterdir()) if f.suffix.lower() in LOADERS]
    chunks = [c for f in files for c in chunk_file(f)]
    body = sum(c.metadata["section"] == "body" for c in chunks)
    print(f"Split {len(files)} files into {len(chunks)} chunks ({body} body, {len(chunks) - body} references)")
    save_chunks(chunks, CHUNKS_DIR)
    print(f"Saved chunks to {CHUNKS_DIR}")

    # Only new and edited files are stored again (lineage, Lampus Phase 1 Step 6).
    stats = sync(CLEAN_COLLECTION, DOCUMENTS_DIR, files, chunk_file, chunk_id, loader="engine.step6_ingest")
    print(f"Stored {stats['chunks']} chunks in collection '{CLEAN_COLLECTION}' (run {stats['run']}: "
          f"{stats['files_changed']} files changed, {stats['chunks_embedded']} chunks embedded, "
          f"{stats['chunks_reused']} reused)")


if __name__ == "__main__":
    main()
