"""Step 10 - Ingest any bot's documents into that bot's own collection.

    python ingest.py bots/project_guide.yaml

Same pipeline as Step 6 (clean loaders, chunking, stable ids), but the folder
and collection come from the bot's configuration. One collection per bot keeps
tenants apart: a search can only ever see the documents of the bot it serves.

Supported: .pdf and .docx (Step 6 loaders) and .md (split at headings, so each
chunk knows its section, which is shown when citing).
"""

import re
import sys
from pathlib import Path

from langchain_core.documents import Document

from bot_config import Knowledge, load_bot
from step1_ingest import split_documents
from step6_ingest import load_docx, load_pdf
from vectorstore import get_vectorstore

HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


def load_markdown(path: Path) -> list[Document]:
    """One Document per heading section, carrying the heading as metadata."""
    docs, heading, lines = [], path.stem, []

    def flush():
        text = "\n".join(lines).strip()
        if text:
            docs.append(Document(page_content=f"{heading}\n{text}",
                                 metadata={"source": str(path), "heading": heading, "section": "body"}))

    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        in_code ^= line.strip().startswith("```")
        match = None if in_code else HEADING.match(line)
        if match:
            flush()
            heading, lines = match.group(2), []
        else:
            lines.append(line)
    flush()
    return docs


LOADERS = {".pdf": load_pdf, ".docx": load_docx, ".md": load_markdown}


def chunk_id(chunk: Document) -> str:
    meta = chunk.metadata
    where = f"p{meta['page'] + 1}" if "page" in meta else re.sub(r"\W+", "-", meta.get("heading", ""))[:40]
    return f"{Path(meta['source']).name}:{where}:{meta['section']}:{meta['start_index']}"


def ingest(knowledge: Knowledge, quiet: bool = False) -> dict:
    files = sorted(f for f in knowledge.path.rglob("*") if f.suffix.lower() in LOADERS)
    if not files:
        raise SystemExit(f"No .pdf, .docx or .md files in {knowledge.folder}")
    docs = []
    for file in files:
        docs.extend(LOADERS[file.suffix.lower()](file))
    chunks = split_documents(docs)
    for chunk in chunks:  # a stable, readable source label: path inside the folder
        chunk.metadata["source"] = str(Path(chunk.metadata["source"]).relative_to(knowledge.path))
    store = get_vectorstore(knowledge.collection)
    store.reset_collection()
    # Chroma accepts only str/int/float/bool metadata values.
    store.add_documents(chunks, ids=[chunk_id(c) for c in chunks])
    stats = {"files": len(files), "chunks": len(chunks), "collection": knowledge.collection}
    if not quiet:
        print(f"Ingested {stats['files']} files as {stats['chunks']} chunks into '{knowledge.collection}'")
    return stats


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python ingest.py bots/<bot>.yaml")
    bot = load_bot(sys.argv[1])
    if bot.knowledge is None:
        raise SystemExit(f"{bot.id} has no knowledge section.")
    ingest(bot.knowledge)
