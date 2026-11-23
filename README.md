# Agentic RAG with LangGraph

A learning project: an agentic system built step by step with LangGraph, where one of the agent's tools is retrieval (RAG) over documents in a vector store. The long-term goal is a chatbot creator, where a use case is turned into a bot with its own prompts, agents, tools and look; the first bot is a study assistant for counselling students. Each step adds one layer of complexity and is mapped to the topics of an AI system design course.

- [Roadmap](docs/ROADMAP.md): all steps, their status and the course topics each one covers.
- Course notes (`docs/COURSE_NOTES.md`): per step, how the course topics show up in the code. Kept locally and not included in the repository.
- [Architecture decision records](docs/adr/README.md): why the main technical choices were made.

## Current status

Step 1 is done: documents are loaded, chunked, embedded and stored in a local vector store, and can be searched by meaning.

Step 2 is done: a minimal LangGraph chatbot using an LLM through OpenRouter.

Step 3 is done: a study assistant agent that decides when to search the documents, answers from the retrieved passages and cites pages. It exists in two versions: the agent loop written by hand, and the same loop as a LangGraph graph.

Step 4 is done: a generic agent engine that runs any bot defined in a YAML file, with tools from a registry (including an MCP server) and human approval before tools that change things.

Step 5 is done: conversations are saved to SQLite and can be resumed after a restart, including a pending approval; long conversations are compacted into a summary.

Step 6 is done: cleaner PDF parsing into a second collection, and hybrid search (meaning plus keywords) with reranking, measured against the old pipeline. The agents from Steps 3 to 5 now use this retrieval.

Step 7 is done: agentic RAG. Searches grade their own results and retry with a rewritten query, and each final answer is fact-checked against its sources, with one revision if it fails.

## Setup

Requires Python 3.11 on Windows (the pinned requirements include Windows-only packages; see [ADR 0004](docs/adr/0004-pin-all-dependency-versions.md)).

```bash
python -m venv .venv
```

```bash
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The embedding model downloads automatically on the first run.

From Step 2 an OpenRouter API key is needed. Copy `.env.example` to `.env` and put the key in it (create one at https://openrouter.ai/keys). `.env` stays on your machine and must never be shared.

## Step 1: ingestion and search

Put PDF or DOCX files in `documents/` (the repository ships without documents; check a document's licence before committing it), then build the vector store:

```bash
.\.venv\Scripts\python.exe step1_ingest.py
```

Search it (interactive if no question is given):

```bash
.\.venv\Scripts\python.exe step1_search.py "What are the common factors of change?"
```

The same logic is also available as one file, `step1_all_in_one.py`, with `ingest` and `search` commands.

## Step 2: first LangGraph graph

```bash
.\.venv\Scripts\python.exe step2_chatbot.py
```

Each reply shows time to first token, total time and token counts. To print the graph as a Mermaid diagram:

```bash
.\.venv\Scripts\python.exe step2_chatbot.py --graph
```

## Step 3: retrieval as a tool

The hand-written agent loop, no LangGraph:

```bash
.\.venv\Scripts\python.exe step3a_manual_loop.py "What are the common factors of change?"
```

The same agent as a LangGraph graph (add `--graph` to print the diagram):

```bash
.\.venv\Scripts\python.exe step3b_langgraph_agent.py "What are the common factors of change?"
```

Leave the question off for an interactive chat. Both print each tool call and the token counts of every LLM call.

## Step 4: bot configuration, MCP tools and human approval

```bash
.\.venv\Scripts\python.exe step4_agent.py
```

It loads `bots/study_assistant.yaml` by default (`--bot` picks another file; `--graph` prints the diagram). Ask it to save a note to see the approval prompt; approved notes are written to `notes/`.

## Step 5: memory

```bash
.\.venv\Scripts\python.exe step5_agent.py
```

Each conversation gets an id. Resume it later with `--thread <id>`, or list saved conversations with `--list`. After each answer the memory size is shown, and a line appears when older turns are compacted into a summary.

## Step 6: better retrieval

Build the cleaned collection (required since Step 6: the agents' search tool reads it):

```bash
.\.venv\Scripts\python.exe step6_ingest.py
```

Compare the retrieval methods on fixed questions (hit@4, MRR, latency):

```bash
.\.venv\Scripts\python.exe step6_compare.py
```

The reranker model downloads automatically on the first run.

## Step 7: agentic RAG

```bash
.\.venv\Scripts\python.exe step7_agent.py
```

Each search prints its attempts and the grader's verdict, and each answer shows whether it passed the fact check. `--graph` prints both the agent graph and the search subgraph.

## Project layout

| Path | Contents |
|---|---|
| `documents/` | Source documents (PDF, DOCX) |
| `vectorstore.py` | Shared embedding model and Chroma connection, used by ingestion and search |
| `step1_ingest.py` | Load, split, embed and store |
| `step1_search.py` | Similarity search over the stored chunks |
| `step1_all_in_one.py` | Step 1 in a single file |
| `llm.py` | Shared chat model (OpenRouter), configured from `.env` |
| `step2_chatbot.py` | Minimal LangGraph chatbot: state, node, edges, streaming |
| `tools.py` | Tools the agent can call (`search_documents`) |
| `prompts.py` | System prompts; the product's domain lives here |
| `step3a_manual_loop.py` | The agent loop in plain Python |
| `step3b_langgraph_agent.py` | The same agent loop as a LangGraph graph |
| `bots/` | Bot definitions: name, theme, prompt, tools, which tools need approval |
| `bot_config.py` | Loads and validates a bot file |
| `tool_registry.py` | Every tool a bot may use: local tools and MCP servers |
| `mcp_servers/notes_server.py` | MCP server offering `save_note` and `list_notes` |
| `step4_agent.py` | Generic agent engine with human approval |
| `step5_agent.py` | The engine with persistent memory and compaction |
| `data/` | Generated: `checkpoints.sqlite`, the saved conversations |
| `step6_ingest.py` | Cleaner ingestion (PyMuPDF, no footers, reference lists tagged) into `documents_clean` |
| `retrieval.py` | Vector, keyword (BM25), hybrid and reranked search |
| `step6_compare.py` | Measures the retrieval methods against each other |
| `search_graph.py` | Graded search subgraph behind the `search_course_material` tool |
| `step7_agent.py` | The engine with answer fact-checking |
| `chunks_clean/` | Generated: the cleaned chunks, for inspection |
| `notes/` | Generated: notes saved by the agent |
| `.env.example` | Template for `.env` (API key and model) |
| `chunks/` | Generated: every chunk as a text file, for inspection |
| `chroma_db/` | Generated: the persisted vector store |
| `docs/` | Roadmap, course notes and ADRs |

## Licence

The code is released under the [MIT License](LICENSE). Source documents are not included and keep their own licences.
