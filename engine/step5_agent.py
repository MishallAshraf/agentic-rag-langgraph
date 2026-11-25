"""Step 5 - Memory: conversations live in the graph and survive restarts.

New compared with Step 4:
  - a SQLite CHECKPOINTER saves the state after every node, per conversation
    ("thread"). We now send only the NEW message each turn; the graph already
    has the history. Quit, come back with --thread, and carry on.
  - a "compact" node keeps long conversations within budget: when the history
    gets too big, older turns are summarised and removed from the messages.

    START --> compact --> agent --route--> tools / human_approval / END
                            ^                 |
                            +-----------------+

    python -m engine.step5_agent                   # new conversation
    python -m engine.step5_agent --thread abc123   # continue a conversation
    python -m engine.step5_agent --list            # list saved conversations
    python -m engine.step5_agent --graph           # print the graph as Mermaid
"""

import argparse
import asyncio
import os
import uuid
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from engine.db import postgres_checkpointer
from engine.bot_config import BotConfig, load_bot
from engine.step4_agent import ask_human, print_update
from engine.tool_registry import load_tools

DEFAULT_BOT = "bots/study_assistant.yaml"
# The SQLite file conversations were saved in before Lampus Phase 1, Step 4 (now Postgres);
# kept as the default source for python -m engine.migrate_sqlite_checkpoints.
CHECKPOINT_DB = Path(os.getenv("CHECKPOINT_DB", str(Path(__file__).resolve().parent.parent / "data" / "checkpoints.sqlite")))
RECURSION_LIMIT = 12

# Compaction policy. Deliberately small so you can watch it happen.
COMPACT_ABOVE_TOKENS = 4000  # approximate size of the message history
KEEP_LAST_TURNS = 2  # the current question plus the previous turn stay verbatim

SUMMARY_PROMPT = """You maintain a running summary of a conversation between a user and an assistant.
Merge the existing summary with the new part of the conversation into one concise summary.
Keep what the user asked, the key points of the answers with their citations, and any preferences the user stated.
Do not copy long passages. Write plain prose, at most 200 words."""


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    # A plain field without a reducer: a node's update simply replaces it.
    summary: NotRequired[str]


def make_compact(llm):
    """Build the compaction node. Shared with later steps."""

    def compact(state):
        messages = state["messages"]
        if count_tokens_approximately(messages) <= COMPACT_ABOVE_TOKENS:
            return {}
        turn_starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
        if len(turn_starts) <= KEEP_LAST_TURNS:
            return {}
        # Cut only at the start of a turn, so a tool call is never separated
        # from its result (the API rejects a tool call without a result).
        cut = turn_starts[-KEEP_LAST_TURNS]
        old = messages[:cut]
        # Summarise the visible conversation only. Retrieved passages (tool
        # results) are the bulk of the tokens and can be fetched again if needed.
        transcript = "\n".join(
            f"{'Student' if isinstance(m, HumanMessage) else 'Assistant'}: {m.text}"
            for m in old
            if isinstance(m, (HumanMessage, AIMessage)) and m.text
        )
        summary = llm.invoke([
            SystemMessage(SUMMARY_PROMPT),
            HumanMessage(f"Existing summary:\n{state.get('summary') or '(none)'}\n\n"
                         f"New part of the conversation:\n{transcript}"),
        ]).text
        # RemoveMessage tells the add_messages reducer to DELETE by id.
        return {"summary": summary, "messages": [RemoveMessage(id=m.id) for m in old]}

    return compact


def build_graph(bot: BotConfig, tools: list, llm, checkpointer):
    llm_with_tools = llm.bind_tools(tools) if llm is not None else None
    needs_approval = set(bot.approval_required)

    compact = make_compact(llm)

    def agent(state: State):
        prompt = [SystemMessage(bot.system_prompt)]
        if state.get("summary"):
            prompt.append(SystemMessage(f"Summary of the earlier conversation:\n{state['summary']}"))
        return {"messages": [llm_with_tools.invoke(prompt + state["messages"])]}

    def route(state: State):
        calls = state["messages"][-1].tool_calls
        if not calls:
            return END
        if any(c["name"] in needs_approval for c in calls):
            return "human_approval"
        return "tools"

    def human_approval(state: State):
        calls = state["messages"][-1].tool_calls
        decision = interrupt({"tool_calls": [c for c in calls if c["name"] in needs_approval]})
        if decision == "approve":
            return Command(goto="tools")
        rejections = [
            ToolMessage(content="The user rejected this tool call.", tool_call_id=c["id"])
            for c in calls
        ]
        return Command(goto="agent", update={"messages": rejections})

    builder = StateGraph(State)
    builder.add_node("compact", compact)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("human_approval", human_approval, destinations=("tools", "agent"))
    builder.add_edge(START, "compact")
    builder.add_edge("compact", "agent")
    builder.add_conditional_edges("agent", route, ["tools", "human_approval", END])
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


def thread_config(bot: BotConfig, thread: str) -> dict:
    # Prefix with the bot id so two bots can never read each other's threads.
    return {"configurable": {"thread_id": f"{bot.id}:{thread}"}, "recursion_limit": RECURSION_LIMIT}


async def drive(graph, graph_input, config):
    """Run the graph, handling approval pauses, until it finishes."""
    while True:
        interrupted = None
        async for update in graph.astream(graph_input, config, stream_mode="updates"):
            for node, data in update.items():
                if node == "__interrupt__":
                    interrupted = data[0].value
                elif node == "compact":
                    if data:
                        removed = len(data["messages"])
                        print(f"  [compact: summarised and removed {removed} older messages]")
                else:
                    print_update(node, data)
        if interrupted is None:
            return
        graph_input = Command(resume=ask_human(interrupted))


async def resume_if_paused(graph, config):
    """A pause is saved too: finish an approval left open in an earlier session."""
    snapshot = await graph.aget_state(config)
    if snapshot.interrupts:
        print("This conversation was paused waiting for an approval.")
        await drive(graph, Command(resume=ask_human(snapshot.interrupts[0].value)), config)
        await print_answer(graph, config)


async def print_answer(graph, config):
    state = (await graph.aget_state(config)).values
    messages = state["messages"]
    print(f"\nAI: {messages[-1].text}")
    print(f"  [memory: {len(messages)} messages, ~{count_tokens_approximately(messages)} tokens"
          f"{', plus a summary' if state.get('summary') else ''}]")


async def list_threads(checkpointer, bot: BotConfig):
    latest = {}
    async for checkpoint in checkpointer.alist(None):
        thread_id = checkpoint.config["configurable"]["thread_id"]
        if thread_id.startswith(f"{bot.id}:") and thread_id not in latest:
            latest[thread_id] = checkpoint
    if not latest:
        print("No saved conversations.")
    for thread_id, checkpoint in latest.items():
        messages = checkpoint.checkpoint["channel_values"].get("messages", [])
        first = next((m.text for m in messages if isinstance(m, HumanMessage)), "(summarised)")
        print(f"{thread_id.split(':', 1)[1]}  {len(messages):>3} messages  {first[:60]}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", default=DEFAULT_BOT)
    parser.add_argument("--thread", help="continue this conversation")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--graph", action="store_true")
    args = parser.parse_args()

    bot = load_bot(args.bot)
    async with postgres_checkpointer() as checkpointer:
        if args.list:
            await list_threads(checkpointer, bot)
            return
        tools = await load_tools(bot.tools, bot)
        if args.graph:
            print(build_graph(bot, tools, None, checkpointer).get_graph().draw_mermaid())
            return

        from engine.llm import get_llm

        graph = build_graph(bot, tools, get_llm(), checkpointer)
        thread = args.thread or uuid.uuid4().hex[:6]
        config = thread_config(bot, thread)

        print(f"{bot.name}  (conversation {thread}; resume with --thread {thread})")
        if args.thread:
            await resume_if_paused(graph, config)
        else:
            print(bot.welcome)
        print("(blank line to quit)")
        try:
            while True:
                text = input("\nYou: ").strip()
                if not text:
                    break
                # Only the new message: the checkpointer supplies the history.
                await drive(graph, {"messages": [HumanMessage(text)]}, config)
                await print_answer(graph, config)
        except (EOFError, KeyboardInterrupt):
            print(f"\nStopped. Resume with --thread {thread}")


if __name__ == "__main__":
    asyncio.run(main())
