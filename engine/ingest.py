"""Step 10 - Ingest any bot's documents into that bot's own collection.

    python -m engine.ingest bots/project_guide.yaml

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

from engine.bot_config import Knowledge, load_bot
from engine.lineage import sync
from engine.step1_ingest import split_documents
from engine.step6_ingest import load_docx, load_pdf

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

    def chunk_file(file: Path) -> list[Document]:
        chunks = split_documents(LOADERS[file.suffix.lower()](file))
        for chunk in chunks:  # a stable, readable source label: path inside the folder
            chunk.metadata["source"] = str(Path(chunk.metadata["source"]).relative_to(knowledge.path))
        return chunks

    # Step 6 of Lampus Phase 1: only new and edited files are re-chunked, and only
    # chunks whose text changed are embedded again; every chunk records its lineage.
    stats = sync(knowledge.collection, knowledge.path, files, chunk_file, chunk_id, loader="engine.ingest")
    stats.update(files=len(files), collection=knowledge.collection)
    if not quiet:
        print(f"Ingested {stats['files']} files as {stats['chunks']} chunks into '{knowledge.collection}' "
              f"(run {stats['run']}: {stats['files_changed']} files changed, {stats['files_removed']} removed; "
              f"{stats['chunks_embedded']} chunks embedded, {stats['chunks_reused']} reused)")
    return stats


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m engine.ingest bots/<bot>.yaml")
    bot = load_bot(sys.argv[1])
    if bot.knowledge is None:
        raise SystemExit(f"{bot.id} has no knowledge section.")
    ingest(bot.knowledge)
