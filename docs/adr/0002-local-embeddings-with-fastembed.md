# 0002. Local embeddings with fastembed

- Status: Accepted
- Date: 2026-10-10
- Step: 1

## Context

Every chunk, and every question, must be turned into a vector. No embedding API key is configured, the machine has 8 GB RAM and no GPU, and the documents are English.

## Decision

Use `FastEmbedEmbeddings` with `BAAI/bge-small-en-v1.5`: a small English model producing 384-dimensional vectors, run locally on the CPU through ONNX. It is defined once in `vectorstore.py` so ingestion and search always use the same model.

## Alternatives considered

- **sentence-transformers:** the most common local option, but depends on PyTorch, a large install and heavier on 8 GB RAM.
- **Embedding APIs (OpenAI, Voyage, Cohere and similar):** often higher quality, but need an account and key, cost money per token, and send document text off the machine.
- **Larger local models (for example `bge-base`, 768 dimensions):** somewhat better retrieval, but slower and more memory for a small gain on a 22-page document.

## Consequences

- Free, offline after the first download, and no data leaves the machine.
- The model downloads automatically on the first run.
- Changing the embedding model invalidates every stored vector; the whole collection must be re-ingested.
- English only. Documents in other languages would need a multilingual model.
- Quality is adequate for learning; whether a better model is worth it should be decided with the evaluation harness from Step 8.
