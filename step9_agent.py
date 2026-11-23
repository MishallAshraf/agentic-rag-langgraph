"""Step 9a - The Step 7 agent inside guardrails.

The Step 7 agent becomes a subgraph, with a check on each side of it:

    START --> guard_input --ok--------> assistant (Step 7 graph) --> guard_output --> END
                  |                                                      |
                  +--crisis----> fixed reply with crisis services --> END
                  +--injection-> fixed refusal ---------------------> END
                                                        (leaked prompt -> reply replaced)

Inside the assistant, every tool result is wrapped in <tool_output> tags and
the system prompt carries the data rule and a canary (see guardrails.py).

    python step9_agent.py                   # new conversation
    python step9_agent.py --thread abc123   # continue a conversation
    python step9_agent.py --graph           # print the graph as Mermaid

Step 9b adds a step limit per turn and recovery: if a turn fails (model down
after retries and fallbacks, step limit reached), the saved conversation is
repaired and the student gets a short apology instead of a crash.
"""

import argparse
import asyncio
import uuid
from typing import NotRequired

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

import step7_agent
from bot_config import BotConfig, load_bot
from guardrails import (CRISIS_REPLY, INJECTION_REPLY, amark_as_data, leaks, make_input_check,
                        mark_as_data, new_canary, with_canary)
from step4_agent import ask_human, print_update
from step5_agent import CHECKPOINT_DB, list_threads, thread_config
from step7_agent import DEFAULT_BOT, print_answer
from tool_registry import load_tools


# Graph steps per turn. A normal turn takes 6-15; the limit stops a runaway
# loop (the model calling tools over and over) before it burns money.
MAX_STEPS_PER_TURN = 40
FAILURE_REPLY = ("Sorry, something went wrong on my side and I couldn't finish that answer. "
                 "Please try again in a moment.")


class State(step7_agent.State):
    guard: NotRequired[str]  # the input check's verdict for the current turn


def build_graph(bot: BotConfig, tools: list, llm, checkpointer, guard_llm=None):
    canary = new_canary()
    hardened_bot = bot.model_copy(update={"system_prompt": with_canary(bot.system_prompt, canary)})
    tool_node = ToolNode(tools, wrap_tool_call=mark_as_data, awrap_tool_call=amark_as_data)
    # Compiled without a checkpointer: as a subgraph it uses the parent's, so an
    # approval pause inside it is saved and resumed like any other.
    assistant = step7_agent.build_graph(hardened_bot, tools, llm, None, tool_node=tool_node)
    check_input = make_input_check(guard_llm or llm) if llm is not None else None

    async def guard_input(state: State):
        question = state["messages"][-1].text
        category, reason = await check_input(question)
        if category == "crisis":
            return Command(goto=END, update={"guard": f"crisis: {reason}",
                                             "messages": [AIMessage(CRISIS_REPLY)]})
        if category == "injection":
            return Command(goto=END, update={"guard": f"injection: {reason}",
                                             "messages": [AIMessage(INJECTION_REPLY)]})
        return Command(goto="assistant", update={"guard": "ok"})

    def guard_output(state: State):
        answer = state["messages"][-1]
        if isinstance(answer, AIMessage) and leaks(answer.text, canary):
            return {"guard": "output blocked: system prompt leak",
                    "messages": [RemoveMessage(id=answer.id), AIMessage(INJECTION_REPLY)]}
        return {}

    builder = StateGraph(State)
    builder.add_node("guard_input", guard_input, destinations=("assistant", END))
    builder.add_node("assistant", assistant)
    builder.add_node("guard_output", guard_output)
    builder.add_edge(START, "guard_input")
    builder.add_edge("assistant", "guard_output")
    builder.add_edge("guard_output", END)
    return builder.compile(checkpointer=checkpointer)


async def drive(graph, graph_input, config):
    while True:
        interrupted = None
        # subgraphs=True also streams the updates from inside the assistant subgraph.
        async for namespace, update in graph.astream(graph_input, config, stream_mode="updates",
                                                     subgraphs=True):
            for node, data in update.items():
                if node == "__interrupt__":
                    interrupted = data[0].value
                elif node in ("guard_input", "guard_output"):
                    if data and data.get("guard"):
                        print(f"  [{node}: {data['guard']}]")
                elif node == "assistant" or not data:
                    continue  # the subgraph's final state; its steps were already printed
                elif node == "start_turn":
                    if data.get("messages"):
                        print(f"  [compact: summarised and removed {len(data['messages'])} older messages]")
                elif node == "check_answer":
                    print("  [check_answer: " + ("NOT supported, answer removed]" if data.get("feedback")
                                                 else "supported]"))
                else:
                    print_update(node, data)
        if interrupted is None:
            return
        graph_input = Command(resume=ask_human(interrupted))


def turn_config(bot: BotConfig, thread: str) -> dict:
    return {**thread_config(bot, thread), "recursion_limit": MAX_STEPS_PER_TURN}


async def recover(graph, config, error: Exception):
    """Leave the conversation usable after a failed turn.

    A turn can fail between the model asking for a tool and the tool's result.
    The saved history then ends with tool calls that have no answer, and every
    later request would be rejected by the model API. So: answer any open tool
    calls with an error, add an apology, and mark the turn as finished.
    """
    messages = (await graph.aget_state(config)).values.get("messages", [])
    fixes = []
    if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        fixes += [ToolMessage(content="This tool call failed and was not completed.", tool_call_id=c["id"])
                  for c in messages[-1].tool_calls if c["id"] not in answered]
    fixes.append(AIMessage(FAILURE_REPLY))
    # as_node="guard_output": record the fix as if the turn had ended normally.
    await graph.aupdate_state(config, {"messages": fixes, "guard": f"error: {type(error).__name__}"},
                              as_node="guard_output")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", default=DEFAULT_BOT)
    parser.add_argument("--thread", help="continue this conversation")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--graph", action="store_true")
    args = parser.parse_args()

    bot = load_bot(args.bot)
    CHECKPOINT_DB.parent.mkdir(exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as checkpointer:
        if args.list:
            await list_threads(checkpointer, bot)
            return
        tools = await load_tools(bot.tools)
        if args.graph:
            print(build_graph(bot, tools, None, checkpointer).get_graph(xray=1).draw_mermaid())
            return

        from llm import get_llm

        graph = build_graph(bot, tools, get_llm(), checkpointer)
        thread = args.thread or uuid.uuid4().hex[:6]
        config = turn_config(bot, thread)
        print(f"{bot.name}  (conversation {thread}; resume with --thread {thread})")
        print(bot.welcome if not args.thread else "")
        print("(blank line to quit)")
        try:
            while True:
                text = input("\nYou: ").strip()
                if not text:
                    break
                try:
                    await drive(graph, {"messages": [HumanMessage(text)]}, config)
                except Exception as e:  # noqa: BLE001 - any failure: repair, apologise, carry on
                    print(f"  [error: {type(e).__name__}: {str(e)[:200]}]")
                    await recover(graph, config, e)
                await print_answer(graph, config)
        except (EOFError, KeyboardInterrupt):
            print(f"\nStopped. Resume with --thread {thread}")


if __name__ == "__main__":
    asyncio.run(main())
