"""Step 4 - A generic agent engine driven by a bot configuration file.

New compared with Step 3:
  - the bot (name, prompt, tools) comes from bots/*.yaml, not from code
  - tools come from a registry, including tools served by an MCP server
  - tools listed under approval_required pause the graph until a human decides

                          +--> human_approval --approve--> tools --+
                          |          |                             |
    START --> agent --route-+          +--reject--> agent          |
                 ^        |                                         |
                 |        +--> tools -------------------------------+
                 |        +--> END (no tool calls)                  |
                 +--------------------------------------------------+

Pausing needs a CHECKPOINTER: when the graph stops at interrupt(), LangGraph
saves the state so it can resume from exactly that point later. Here it only
lives for one question; Step 5 uses it for conversation memory too.

    python -m engine.step4_agent                                  # chat with the default bot
    python -m engine.step4_agent "What are the common factors of change?"
    python -m engine.step4_agent --bot bots/other.yaml            # a different bot
    python -m engine.step4_agent --graph                          # print the graph as Mermaid
"""

import argparse
import asyncio
import uuid
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from engine.bot_config import BotConfig, load_bot
from engine.tool_registry import load_tools

DEFAULT_BOT = "bots/study_assistant.yaml"
RECURSION_LIMIT = 12


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_graph(bot: BotConfig, tools: list, llm):
    llm_with_tools = llm.bind_tools(tools) if llm is not None else None
    needs_approval = set(bot.approval_required)

    def agent(state: State):
        prompt = [SystemMessage(bot.system_prompt)] + state["messages"]
        return {"messages": [llm_with_tools.invoke(prompt)]}

    def route(state: State):
        """After the agent: answer, run tools, or ask a human first."""
        calls = state["messages"][-1].tool_calls
        if not calls:
            return END
        if any(c["name"] in needs_approval for c in calls):
            return "human_approval"
        return "tools"

    def human_approval(state: State):
        calls = state["messages"][-1].tool_calls
        # interrupt() stops the graph here and hands this value to the caller.
        # When the caller resumes with Command(resume=...), the node runs again
        # from the top and interrupt() returns that resume value instead.
        decision = interrupt({"tool_calls": [c for c in calls if c["name"] in needs_approval]})
        if decision == "approve":
            return Command(goto="tools")
        # Every tool call must get an answer, so reject all calls in this message.
        rejections = [
            ToolMessage(content="The user rejected this tool call.", tool_call_id=c["id"])
            for c in calls
        ]
        return Command(goto="agent", update={"messages": rejections})

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    # `destinations` declares where the Command can send us, so the diagram shows it.
    builder.add_node("human_approval", human_approval, destinations=("tools", "agent"))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, ["tools", "human_approval", END])
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=InMemorySaver())


def print_update(node: str, update):
    if not update:
        return
    for msg in update.get("messages", []):
        if node == "agent":
            usage = msg.usage_metadata or {}
            print(f"  [agent: tokens in {usage.get('input_tokens', '?')}, "
                  f"out {usage.get('output_tokens', '?')}]")
            for call in msg.tool_calls:
                print(f"  -> tool call: {call['name']}({call['args']})")
        else:
            preview = " ".join(str(msg.content).split())[:100]
            print(f"  <- {node}: {preview}")


def ask_human(request) -> str:
    print("\n  APPROVAL NEEDED")
    for call in request["tool_calls"]:
        print(f"  {call['name']}:")
        for key, value in call["args"].items():
            print(f"    {key}: {value}")
    answer = input("  Approve? [y/N]: ").strip().lower()
    return "approve" if answer in ("y", "yes") else "reject"


async def run_turn(graph, history: list) -> list:
    # A fresh thread per question: the checkpointer is only used for pausing.
    config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": RECURSION_LIMIT}
    graph_input = {"messages": history}

    while True:
        interrupted = None
        async for update in graph.astream(graph_input, config, stream_mode="updates"):
            for node, data in update.items():
                if node == "__interrupt__":
                    interrupted = data[0].value
                else:
                    print_update(node, data)
        if interrupted is None:
            break
        # Resume the paused graph with the human's decision.
        graph_input = Command(resume=ask_human(interrupted))

    messages = (await graph.aget_state(config)).values["messages"]
    print(f"\nAI: {messages[-1].content}")
    return messages


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="*")
    parser.add_argument("--bot", default=DEFAULT_BOT)
    parser.add_argument("--graph", action="store_true")
    args = parser.parse_args()

    bot = load_bot(args.bot)
    tools = await load_tools(bot.tools, bot)

    if args.graph:
        print(build_graph(bot, tools, llm=None).get_graph().draw_mermaid())
        return

    from engine.llm import get_llm

    graph = build_graph(bot, tools, get_llm())
    if args.question:
        await run_turn(graph, [HumanMessage(" ".join(args.question))])
        return

    print(f"{bot.name}\n{bot.welcome}\nTry: " + " | ".join(bot.suggested_questions))
    print("(blank line to quit)")
    history = []
    while True:
        text = input("\nYou: ").strip()
        if not text:
            break
        history = await run_turn(graph, history + [HumanMessage(text)])


if __name__ == "__main__":
    asyncio.run(main())
