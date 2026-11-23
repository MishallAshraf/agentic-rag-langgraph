"""Step 3b - The same agent loop as step3a, built as a LangGraph graph.

                 +-------- tool calls? ---------+
                 |                              v
    START --> agent                           tools
                 |                              |
                 +-- no tool calls --> END      +--> back to agent

What each piece of step3a became:
    the model call        -> the "agent" node
    running the tools     -> the "tools" node (LangGraph's prebuilt ToolNode)
    "any tool calls?"     -> a CONDITIONAL edge (tools_condition)
    the while loop        -> the edge from "tools" back to "agent"
    MAX_ITERATIONS        -> recursion_limit (max number of node runs)

    python step3b_langgraph_agent.py "What are the common factors of change?"
    python step3b_langgraph_agent.py            # interactive
    python step3b_langgraph_agent.py --graph    # print the graph as Mermaid
"""

import sys
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from prompts import STUDY_ASSISTANT
from tools import TOOLS

# Each node run counts as one step: agent, tools, agent, tools, agent = 5.
RECURSION_LIMIT = 10


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_graph(llm):
    llm_with_tools = llm.bind_tools(TOOLS) if llm is not None else None

    def agent(state: State):
        prompt = [SystemMessage(STUDY_ASSISTANT)] + state["messages"]
        return {"messages": [llm_with_tools.invoke(prompt)]}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    # ToolNode runs every tool call in the last AI message and returns ToolMessages.
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "agent")
    # tools_condition looks at the last message: tool calls -> "tools", else -> END.
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile()


def run_turn(graph, history: list) -> list:
    """Run the graph, printing each node's output as it finishes."""
    final_state = None
    try:
        # "updates" yields {node_name: what_that_node_returned} after each node,
        # which shows the agent's path through the graph step by step.
        for mode, data in graph.stream(
            {"messages": history},
            {"recursion_limit": RECURSION_LIMIT},
            stream_mode=["updates", "values"],
        ):
            if mode == "values":
                final_state = data
                continue
            for node, update in data.items():
                for msg in update["messages"]:
                    if node == "agent":
                        usage = msg.usage_metadata or {}
                        print(f"  [agent: tokens in {usage.get('input_tokens', '?')}, "
                              f"out {usage.get('output_tokens', '?')}]")
                        for call in msg.tool_calls:
                            print(f"  -> tool call: {call['name']}({call['args']})")
                    else:
                        print(f"  <- {msg.name} result: {len(msg.content)} characters")
    except GraphRecursionError:
        print(f"\nStopped: hit the recursion limit of {RECURSION_LIMIT} steps.")
        return history

    print(f"\nAI: {final_state['messages'][-1].content}")
    return final_state["messages"]


def main():
    if "--graph" in sys.argv:
        print(build_graph(llm=None).get_graph().draw_mermaid())
        return

    from llm import get_llm

    graph = build_graph(get_llm())
    if len(sys.argv) > 1:
        run_turn(graph, [HumanMessage(" ".join(sys.argv[1:]))])
        return
    history = []
    print("Ask the study assistant (blank line to quit).")
    while True:
        text = input("\nYou: ").strip()
        if not text:
            break
        history = run_turn(graph, history + [HumanMessage(text)])


if __name__ == "__main__":
    main()
