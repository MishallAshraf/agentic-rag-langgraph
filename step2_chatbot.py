"""Step 2 - The smallest useful LangGraph graph: a chatbot with no tools.

    START --> chatbot --> END

Three LangGraph ideas appear here, and every later step builds on them:
  1. STATE  - a typed dict that flows through the graph.
  2. NODES  - plain functions: take the state, return the parts to update.
  3. EDGES  - which node runs next.

    python step2_chatbot.py           # chat
    python step2_chatbot.py --graph   # print the graph as a Mermaid diagram
"""

import sys
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from llm import get_llm

SYSTEM_PROMPT = "You are a helpful assistant. Answer clearly and concisely."


# 1. STATE ------------------------------------------------------------------
# `messages` holds the conversation. The add_messages "reducer" tells LangGraph
# how to merge a node's update into the state: APPEND the returned messages
# instead of replacing the whole list.
class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_graph(llm):
    # 2. NODE ---------------------------------------------------------------
    # Receives the current state, returns ONLY what changed. LangGraph merges it.
    def chatbot(state: State):
        prompt = [SystemMessage(SYSTEM_PROMPT)] + state["messages"]
        reply = llm.invoke(prompt)
        return {"messages": [reply]}

    # 3. EDGES --------------------------------------------------------------
    builder = StateGraph(State)
    builder.add_node("chatbot", chatbot)
    builder.add_edge(START, "chatbot")
    builder.add_edge("chatbot", END)
    # compile() checks the structure and returns a runnable graph.
    return builder.compile()


def run_turn(graph, messages: list[AnyMessage]) -> list[AnyMessage]:
    """Run the graph once, streaming tokens as they arrive. Returns the new history."""
    start = time.perf_counter()
    first_token_at = None
    final_state = None

    # stream_mode "messages" yields tokens as the LLM produces them;
    # "values" yields the full state after each step (the last one is the result).
    for mode, data in graph.stream({"messages": messages}, stream_mode=["messages", "values"]):
        if mode == "messages":
            chunk, _metadata = data
            if isinstance(chunk, AIMessageChunk) and chunk.content:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                print(chunk.content, end="", flush=True)
        else:
            final_state = data

    total = time.perf_counter() - start
    ttft = (first_token_at - start) if first_token_at else total
    reply = final_state["messages"][-1]
    usage = getattr(reply, "usage_metadata", None) or {}
    print(
        f"\n\n[time to first token {ttft:.2f}s | total {total:.2f}s | "
        f"tokens in {usage.get('input_tokens', '?')}, out {usage.get('output_tokens', '?')}]"
    )
    return final_state["messages"]


def main():
    if "--graph" in sys.argv:
        print(build_graph(llm=None).get_graph().draw_mermaid())
        return

    graph = build_graph(get_llm())
    # The graph itself remembers nothing between runs. We keep the history here
    # and pass all of it in on every turn. (Step 5 replaces this with a checkpointer.)
    history: list[AnyMessage] = []
    print("Chat with the model (blank line to quit).")
    while True:
        text = input("\nYou: ").strip()
        if not text:
            break
        print("AI: ", end="")
        history = run_turn(graph, history + [HumanMessage(text)])


if __name__ == "__main__":
    main()
