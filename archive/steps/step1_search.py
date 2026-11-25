"""Step 1b - Retrieval: question -> embedding -> nearest chunks.

No LLM is involved yet. This is the "R" of RAG on its own, so you can see
exactly what the model will later be given as context.

    python -m archive.steps.step1_search "What are the common factors of change?"
    python -m archive.steps.step1_search            # interactive
"""

import sys

from engine.step1_ingest import format_page
from engine.vectorstore import get_vectorstore

TOP_K = 4


def search(store, question: str):
    # The question is embedded with the same model as the chunks, then Chroma
    # returns the k chunks whose vectors are closest. Lower distance = closer.
    results = store.similarity_search_with_score(question, k=TOP_K)
    print(f"\nQ: {question}")
    for rank, (doc, distance) in enumerate(results, start=1):
        page = format_page(doc.metadata)
        text = " ".join(doc.page_content.split())
        print(f"\n[{rank}] distance={distance:.3f}  page={page}")
        print(f"    {text[:300]}...")


def main():
    store = get_vectorstore()
    if len(sys.argv) > 1:
        search(store, " ".join(sys.argv[1:]))
        return
    while True:
        question = input("\nAsk (blank to quit): ").strip()
        if not question:
            break
        search(store, question)


if __name__ == "__main__":
    main()
