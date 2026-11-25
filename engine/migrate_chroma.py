"""Lampus Phase 1, Step 5 - Copy document chunks from Chroma to Postgres (pgvector).

    python -m engine.migrate_chroma                    # every collection in chroma_db/ -> DATABASE_URL
    python -m engine.migrate_chroma --only bot_customer_handbook

Copies each chunk's id, text, metadata and its stored embedding, so nothing is
re-embedded and the vectors are exactly the ones Chroma searched. Then checks
that each collection has the same number of chunks on both sides. Safe to run
again: a chunk already in Postgres is overwritten with the same values.

Re-ingesting from the source documents (python -m engine.ingest bots/<bot>.yaml)
gives the same result; this is quicker and also covers collections whose source
files are not in the repository.
"""

import argparse

from engine.vectorstore import ROOT, VectorStore

BATCH = 500


def main():
    import chromadb  # only needed for this one-time copy

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chroma", default=str(ROOT / "chroma_db"))
    parser.add_argument("--only", nargs="*", help="collection names (default: all)")
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=args.chroma)
    names = args.only or sorted(c.name for c in client.list_collections())
    for name in names:
        source, target = client.get_collection(name), VectorStore(name)
        total = source.count()
        for offset in range(0, total, BATCH):
            data = source.get(include=["documents", "metadatas", "embeddings"], limit=BATCH, offset=offset)
            target.add_embedded(data["ids"], data["documents"], data["metadatas"], data["embeddings"])
        copied = target.count()
        print(f"{name}: {total} chunks in Chroma, {copied} in Postgres {'(match)' if copied == total else '(MISMATCH)'}")


if __name__ == "__main__":
    main()
