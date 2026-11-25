"""Tools the agent can call.

A tool is an ordinary Python function plus a description. The LLM never runs
code itself: it only sees each tool's NAME, DESCRIPTION and ARGUMENTS, and
replies with a request like "call search_documents with query=...". Our code
(the harness) runs the function and sends the result back.

So the docstring below is not just documentation - it IS the prompt that
tells the model when and how to use the tool.
"""

import re
from pathlib import Path

import pymupdf
from langchain_core.tools import StructuredTool, tool
from pydantic import BaseModel, Field

from engine.retrieval import get_retriever
from engine.step1_ingest import format_page
from engine.vectorstore import DOCUMENTS_DIR

TOP_K = 4


@tool
def search_documents(query: str) -> str:
    """Search the course material on counselling and psychotherapy.

    Use this for any question about counselling, psychotherapy, therapeutic
    change, listening skills or related topics. Returns the most relevant
    passages, each labelled with its source file and page number.

    Args:
        query: what to look for, phrased as a short search query.
    """
    # Since Step 6: hybrid search (meaning + keywords) and reranking, over the
    # cleaned collection, with reference lists filtered out. See retrieval.py.
    results = get_retriever().search(query, k=TOP_K)
    if not results:
        return "No relevant passages found."
    passages = []
    for i, doc in enumerate(results, start=1):
        source = Path(doc.metadata.get("source", "unknown")).name
        page = format_page(doc.metadata)
        text = " ".join(doc.page_content.split())
        passages.append(f"[{i}] {source}, page {page}\n{text}")
    return "\n\n".join(passages)


class ReadPageArgs(BaseModel):
    source: str = Field(description='The file name, for example "Psychotherapy.pdf".')
    page: int = Field(description="The page number, starting at 1.")


def make_read_page(folder: Path, name: str = "read_page") -> StructuredTool:
    """A read-page tool that can only open PDFs in one folder (Step 10: the bot's own)."""

    def read(source: str, page: int) -> str:
        # Model output is untrusted input: allow only a plain file name that exists
        # in this folder (or below it), never a path such as "../.env".
        name = Path(source).name
        if not re.fullmatch(r"[\w .()-]+\.pdf", name, re.IGNORECASE):  # no glob characters like *
            return f"No PDF named '{source}' in the documents."
        matches = [f for f in folder.rglob(name) if f.is_file()]
        if not matches:
            return f"No PDF named '{source}' in the documents."
        pdf = pymupdf.open(matches[0])  # cleaner text than pypdf for this PDF (Step 6)
        if not 1 <= page <= pdf.page_count:
            return f"'{source}' has pages 1 to {pdf.page_count}."
        text = " ".join(pdf[page - 1].get_text().split())
        return f"{matches[0].name}, page {page}\n{text}"

    return StructuredTool.from_function(
        func=read, name=name, args_schema=ReadPageArgs,
        description=("Read the full text of one page of a PDF in the documents. Use this when a "
                     "search passage is cut off or you need the surrounding context. Take source "
                     "and page from the search results."))


# The study assistant's tool, as before: PDFs in documents/.
read_page = make_read_page(DOCUMENTS_DIR)


# Step 3 uses only retrieval.
TOOLS = [search_documents]
