"""Step 7a - Corrective retrieval: a small LangGraph graph used inside a tool.

Plain RAG retrieves once and trusts whatever comes back. Here an LLM grader
checks the passages first:

    START --> retrieve --> grade --+--> END   (enough to answer, or out of attempts)
                 ^                 |
                 +--- new query ---+          (not enough: search again, differently worded)

The grader also drops irrelevant passages, so the agent receives less noise
and fewer tokens. The agent calls all of this as ONE tool; the loop happens
inside it. A graph used inside another graph's node or tool like this is
called a subgraph.

Step 10: the tool is made per bot (make_search_tool), bound to that bot's
collection and subject, so a bot can only ever search its own documents.
"""

import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TypedDict

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from retrieval import CLEAN_COLLECTION, get_retriever
from step1_ingest import format_page

TOP_K = 4
MAX_ATTEMPTS = 3
VERBOSE = True  # print each attempt; the eval runner turns this off
# Step 9: an EXACT cache of graded searches, keyed on the normalised question.
# Not a semantic cache: measured in Step 9, reworded questions scored 0.96-0.98
# similarity, but "does the relationship matter more than technique?" and its
# reverse scored 0.99. No threshold separates right hits from wrong ones.
# Step 10: the key starts with the collection, so bots never share entries.
CACHE_ENABLED = True  # the eval runner turns this off, so cases stay independent
CACHE_SIZE = 256
_cache: OrderedDict[str, str] = OrderedDict()
_cache_lock = threading.Lock()
cache_stats = {"hits": 0, "misses": 0}

STUDY_SUBJECT = "the course material on counselling and psychotherapy"

GRADER_PROMPT = """You check search results before an assistant answers a user's question from {subject}.
Given the user's question and numbered passages:
- list the passages that contain information helping to answer the question;
- decide whether those passages together are enough to answer it;
- if not, write a new search query that could find what is missing. Use different
  wording or specific terms, not a copy of the earlier queries."""


class Grade(BaseModel):
    """The grader's verdict. with_structured_output makes the LLM fill this in."""

    relevant: list[int] = Field(description="Numbers of the passages that help answer the question.")
    sufficient: bool = Field(description="True if the relevant passages together answer the question.")
    next_query: str = Field(description="If not sufficient: a new, differently worded search query. Otherwise empty.")


class SearchState(TypedDict):
    question: str  # what the user wants to know
    query: str  # the current search query
    queries: list[str]  # every query tried so far
    passages: list[Document]  # relevant passages kept so far
    sufficient: bool
    log: list[str]


def label(doc: Document) -> str:
    name = Path(doc.metadata.get("source", "unknown")).name
    if "heading" in doc.metadata:  # Markdown documents have sections, not pages
        return f'{name}, section "{doc.metadata["heading"]}"'
    return f"{name}, page {format_page(doc.metadata)}"


def build_search_graph(llm, collection: str = CLEAN_COLLECTION, subject: str = STUDY_SUBJECT):
    # method="function_calling" asks for the schema as a tool call, which works
    # across the models OpenRouter offers.
    grader = llm.with_structured_output(Grade, method="function_calling") if llm else None
    prompt = GRADER_PROMPT.format(subject=subject)

    def retrieve(state: SearchState):
        found = get_retriever(collection).search(state["query"], k=TOP_K)
        seen = {d.id for d in state["passages"]}
        new = [d for d in found if d.id not in seen]
        return {"passages": state["passages"] + new, "queries": state["queries"] + [state["query"]]}

    def grade(state: SearchState):
        listing = "\n\n".join(
            f"[{i}] {label(d)}\n{' '.join(d.page_content.split())}"
            for i, d in enumerate(state["passages"], start=1)
        )
        verdict = grader.invoke([
            SystemMessage(prompt),
            HumanMessage(f"Question: {state['question']}\n\n"
                         f"Queries tried: {state['queries']}\n\nPassages:\n{listing}"),
        ])
        kept = [d for i, d in enumerate(state["passages"], start=1) if i in verdict.relevant]
        entry = (f"attempt {len(state['queries'])}: '{state['query']}' -> "
                 f"{len(kept)} relevant, enough: {'yes' if verdict.sufficient else 'no'}")
        if VERBOSE:
            print(f"    [search] {entry}")
        update = {"passages": kept, "sufficient": verdict.sufficient, "log": state["log"] + [entry]}
        if verdict.next_query.strip():
            update["query"] = verdict.next_query.strip()
        return update

    def decide(state: SearchState):
        if state["sufficient"] or len(state["queries"]) >= MAX_ATTEMPTS:
            return END
        if state["query"] in state["queries"]:
            return END  # the grader had no new idea; searching again would repeat itself
        return "retrieve"

    builder = StateGraph(SearchState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("grade", grade)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "grade")
    builder.add_conditional_edges("grade", decide, ["retrieve", END])
    return builder.compile()


_graphs: dict[str, object] = {}
_graphs_lock = threading.Lock()


def get_search_graph(collection: str = CLEAN_COLLECTION, subject: str = STUDY_SUBJECT):
    with _graphs_lock:
        if collection not in _graphs:
            from llm import get_llm

            _graphs[collection] = build_search_graph(get_llm(), collection, subject)
    return _graphs[collection]


def cache_key(collection: str, question: str) -> str:
    """Collection, then the question lowercased without punctuation: 'What is X?' == 'what is x'."""
    return collection + "|" + " ".join(re.findall(r"[a-z0-9']+", question.lower()))


def search(collection: str, subject: str, question: str, query: str) -> str:
    key = cache_key(collection, question)
    if CACHE_ENABLED:
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)  # least recently used is evicted first
                cache_stats["hits"] += 1
                return _cache[key]
            cache_stats["misses"] += 1
    output = _search(collection, subject, question, query)
    if CACHE_ENABLED:
        with _cache_lock:
            _cache[key] = output
            if len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return output


def _search(collection: str, subject: str, question: str, query: str) -> str:
    result = get_search_graph(collection, subject).invoke({
        "question": question, "query": query, "queries": [], "passages": [],
        "sufficient": False, "log": [],
    })
    log = "Search log:\n" + "\n".join(f"- {e}" for e in result["log"])
    if not result["passages"]:
        return f"{log}\n\nNo passage in {subject} answers this question."
    status = ("These passages answer the question." if result["sufficient"]
              else "These passages only partly answer the question; say what is missing.")
    passages = "\n\n".join(
        f"[{i}] {label(d)}\n{' '.join(d.page_content.split())}"
        for i, d in enumerate(result["passages"], start=1)
    )
    return f"{log}\n\n{status}\n\n{passages}"


class SearchArgs(BaseModel):
    question: str = Field(description="The user's question that the passages should answer.")
    query: str = Field(description="A short search query to start with.")


def make_search_tool(collection: str, subject: str, name: str = "search_knowledge") -> StructuredTool:
    """A graded-search tool bound to one collection. The model cannot pick another."""
    return StructuredTool.from_function(
        func=lambda question, query: search(collection, subject, question, query),
        name=name,
        args_schema=SearchArgs,
        description=(f"Search {subject}, checking that the results actually answer the question and "
                     "searching again if not. Use this for any question about that material. Returns "
                     "only the relevant passages, each labelled with its source, plus a short log of "
                     "the searches made."),
    )


# The Step 7 study assistant's tool, unchanged in name and behaviour.
search_course_material = make_search_tool(CLEAN_COLLECTION, STUDY_SUBJECT, name="search_course_material")
