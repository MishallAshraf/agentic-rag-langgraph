"""Tools the agent can call.

A tool is an ordinary Python function plus a description. The LLM never runs
code itself: it only sees each tool's NAME, DESCRIPTION and ARGUMENTS, and
replies with a request like "call search_documents with query=...". Our code
(the harness) runs the function and sends the result back.

So the docstring below is not just documentation - it IS the prompt that
tells the model when and how to use the tool.
"""

from pathlib import Path

import pymupdf
from langchain_core.tools import tool

from retrieval import get_retriever
from step1_ingest import format_page
from vectorstore import DOCUMENTS_DIR

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


@tool
def read_page(source: str, page: int) -> str:
    """Read the full text of one page of a PDF in the course material.

    Use this when a passage from search_documents is cut off or you need the
    surrounding context. Take source and page from the search results.

    Args:
        source: the file name, for example "Psychotherapy.pdf".
        page: the page number, starting at 1.
    """
    # Model output is untrusted input: allow only a plain file name that exists
    # in the documents folder, never a path such as "../.env".
    path = DOCUMENTS_DIR / Path(source).name
    if path.suffix.lower() != ".pdf" or not path.is_file():
        return f"No PDF named '{source}' in the course material."
    pdf = pymupdf.open(path)  # cleaner text than pypdf for this PDF (Step 6)
    if not 1 <= page <= pdf.page_count:
        return f"'{source}' has pages 1 to {pdf.page_count}."
    text = " ".join(pdf[page - 1].get_text().split())
    return f"{path.name}, page {page}\n{text}"


# Step 3 uses only retrieval.
TOOLS = [search_documents]
