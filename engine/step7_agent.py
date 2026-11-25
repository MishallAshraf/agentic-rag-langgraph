"""Step 7 - Agentic RAG: graded retrieval and a fact check on every answer.

New compared with Step 5:
  - the bot uses search_course_material, a tool that grades its own results
    and searches again when they don't answer the question (search_graph.py)
  - a check_answer node verifies the final answer against the sources found
    in this turn. Unsupported answer -> it is removed, the agent gets the
    checker's feedback and answers once more.

    START --> start_turn --> agent --route--> tools / human_approval
                               ^  |
                               |  +--> check_answer --supported--> END
                               |            |
                               +-- revise --+

    python -m engine.step7_agent                   # new conversation
    python -m engine.step7_agent --thread abc123   # continue a conversation
    python -m engine.step7_agent --graph           # print both graphs as Mermaid
"""

import argparse
import asyncio
import uuid
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import (
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
from pydantic import BaseModel, Field

from engine.db import postgres_checkpointer
from engine.bot_config import BotConfig, load_bot
from engine.search_graph import build_search_graph
from engine.step4_agent import ask_human, print_update
from engine.step5_agent import list_threads, make_compact, thread_config
from engine.tool_registry import load_tools
from engine.llm import STRUCTURED_METHOD

DEFAULT_BOT = "bots/study_assistant.yaml"
SOURCE_TOOLS = {"search_course_material", "search_knowledge", "search_documents", "read_page"}
MAX_REVISIONS = 1  # one retry after a failed fact check, then accept the answer

CHECKER_PROMPT = """You fact-check an assistant's answer before the user sees it.
You get the sources the assistant retrieved and its answer.
The answer is supported only if every factual claim in it appears in the sources and each page citation points to a source that contains the claim.
Saying that the material does not cover something is always allowed.
Ignore style; judge only the facts and citations."""


class Verdict(BaseModel):
    supported: bool = Field(description="True if every factual claim is backed by the sources.")
    problems: str = Field(description="If not supported: which claims are unsupported or wrongly cited. Otherwise empty.")


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: NotRequired[str]
    feedback: NotRequired[str]  # the checker's objections, shown to the agent once
    revisions: NotRequired[int]  # failed fact checks in the current turn


def current_turn(messages):
    last_question = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    return messages[last_question:]


def build_graph(bot: BotConfig, tools: list, llm, checkpointer, tool_node=None):
    # tool_node: Step 9 passes a ToolNode that marks tool output as data.
    llm_with_tools = llm.bind_tools(tools) if llm is not None else None
    checker = llm.with_structured_output(Verdict, method=STRUCTURED_METHOD) if llm else None
    compact = make_compact(llm)
    needs_approval = set(bot.approval_required)

    def start_turn(state: State):
        # Compact long histories (Step 5) and reset the per-turn revision budget.
        return {**compact(state), "revisions": 0, "feedback": ""}

    def agent(state: State):
        prompt = [SystemMessage(bot.system_prompt)]
        if state.get("summary"):
            prompt.append(SystemMessage(f"Summary of the earlier conversation:\n{state['summary']}"))
        prompt += state["messages"]
        if state.get("feedback"):
            prompt.append(SystemMessage(
                "Your previous answer failed a fact check:\n" + state["feedback"]
                + "\nAnswer again using only the sources above. Remove or correct the "
                "unsupported claims; search again if you need to."))
        return {"messages": [llm_with_tools.invoke(prompt)], "feedback": ""}

    def route(state: State):
        messages = state["messages"]
        calls = messages[-1].tool_calls
        if calls:
            if any(c["name"] in needs_approval for c in calls):
                return "human_approval"
            return "tools"
        used_sources = any(isinstance(m, ToolMessage) and m.name in SOURCE_TOOLS
                           for m in current_turn(messages))
        if bot.verify_answers and used_sources and state.get("revisions", 0) < MAX_REVISIONS:
            return "check_answer"
        return END

    def check_answer(state: State):
        turn = current_turn(state["messages"])
        answer = turn[-1]
        sources = "\n\n".join(m.text for m in turn if isinstance(m, ToolMessage) and m.name in SOURCE_TOOLS)
        verdict = checker.invoke([
            SystemMessage(CHECKER_PROMPT),
            HumanMessage(f"Sources:\n{sources}\n\nAnswer to check:\n{answer.text}"),
        ])
        if verdict.supported:
            return Command(goto=END, update={"feedback": ""})
        # Remove the failed answer so it never reaches the user or the history.
        return Command(goto="agent", update={
            "messages": [RemoveMessage(id=answer.id)],
            "feedback": verdict.problems,
            "revisions": state.get("revisions", 0) + 1,
        })

    def human_approval(state: State):
        calls = state["messages"][-1].tool_calls
        decision = interrupt({"tool_calls": [c for c in calls if c["name"] in needs_approval]})
        if decision == "approve":
            return Command(goto="tools")
        rejections = [ToolMessage(content="The user rejected this tool call.", tool_call_id=c["id"])
                      for c in calls]
        return Command(goto="agent", update={"messages": rejections})

    builder = StateGraph(State)
    builder.add_node("start_turn", start_turn)
    builder.add_node("agent", agent)
    builder.add_node("tools", tool_node or ToolNode(tools))
    builder.add_node("human_approval", human_approval, destinations=("tools", "agent"))
    builder.add_node("check_answer", check_answer, destinations=("agent", END))
    builder.add_edge(START, "start_turn")
    builder.add_edge("start_turn", "agent")
    builder.add_conditional_edges("agent", route, ["tools", "human_approval", "check_answer", END])
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


async def drive(graph, graph_input, config):
    while True:
        interrupted = None
        async for update in graph.astream(graph_input, config, stream_mode="updates"):
            for node, data in update.items():
                if node == "__interrupt__":
                    interrupted = data[0].value
                elif node == "start_turn":
                    if data and data.get("messages"):
                        print(f"  [compact: summarised and removed {len(data['messages'])} older messages]")
                elif node == "check_answer":
                    if data.get("feedback"):
                        print(f"  [check_answer: NOT supported, answer removed]\n    {data['feedback']}")
                    else:
                        print("  [check_answer: supported]")
                else:
                    print_update(node, data)
        if interrupted is None:
            return
        graph_input = Command(resume=ask_human(interrupted))


async def print_answer(graph, config):
    state = (await graph.aget_state(config)).values
    messages = state["messages"]
    print(f"\nAI: {messages[-1].text}")
    print(f"  [memory: {len(messages)} messages, ~{count_tokens_approximately(messages)} tokens"
          f"{', plus a summary' if state.get('summary') else ''}]")


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
            print("%% Agent graph")
            print(build_graph(bot, tools, None, checkpointer).get_graph().draw_mermaid())
            print("%% search_course_material subgraph")
            print(build_search_graph(None).get_graph().draw_mermaid())
            return

        from engine.llm import get_llm

        graph = build_graph(bot, tools, get_llm(), checkpointer)
        thread = args.thread or uuid.uuid4().hex[:6]
        config = thread_config(bot, thread)
        print(f"{bot.name}  (conversation {thread}; resume with --thread {thread})")
        print(bot.welcome if not args.thread else "")
        print("(blank line to quit)")
        try:
            while True:
                text = input("\nYou: ").strip()
                if not text:
                    break
                await drive(graph, {"messages": [HumanMessage(text)]}, config)
                await print_answer(graph, config)
        except (EOFError, KeyboardInterrupt):
            print(f"\nStopped. Resume with --thread {thread}")


if __name__ == "__main__":
    asyncio.run(main())
