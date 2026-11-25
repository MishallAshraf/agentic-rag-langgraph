# 0001. Chroma as the vector store

- Status: Superseded by [ADR 0027](0027-pgvector-as-vector-store.md) (Lampus Phase 1, Step 5)
- Date: 2026-10-10
- Step: 1
- Revisited in: [ADR 0010](0010-hybrid-retrieval-with-reranking.md) (Step 6), which keeps this decision

## Context

Chunk embeddings need to be stored and searched by similarity. The project runs on one Windows laptop (8 GB RAM, no GPU), is a learning project, and will later need metadata filtering: by source in agentic RAG (Step 7) and by access level for authorization (Step 12).

## Decision

Use Chroma in embedded, persistent mode through `langchain-chroma`. It runs inside the Python process and saves to the `chroma_db/` folder. All access goes through `get_vectorstore()` in `vectorstore.py`.

## Alternatives considered

- **FAISS:** very fast, but it is an index library, not a database. Persistence and metadata filtering would have to be built by hand.
- **pgvector:** vectors inside PostgreSQL, strong for production and for combining with relational data, but requires installing and running a database server.
- **Qdrant:** a capable vector database with hybrid search and rich filtering. Its local mode would also work; a stronger candidate if the project outgrows Chroma.
- **Hosted services (Pinecone and similar):** no setup, but adds an account, network latency and sends document text to a third party.

## Consequences

- No server to run; the store is a folder that can be deleted and rebuilt with `step1_ingest.py`.
- Metadata filtering is available for Steps 7 and 12.
- Embedded mode suits one process. When the API (Step 10) runs several workers, Chroma would need to run as a server, or the store would be swapped.
- Keyword search for hybrid retrieval (Step 6) may need to be implemented alongside Chroma. Revisit this decision in Step 6.
- Because code depends on LangChain's vector store interface rather than Chroma directly, swapping stores later is mostly a change to `get_vectorstore()`.
