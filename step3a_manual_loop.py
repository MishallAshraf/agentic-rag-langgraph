"""Step 3a - The agent loop written by hand, in plain Python. No LangGraph.

This is the core of every agent harness:

    1. send the conversation (plus the list of tools) to the model
    2. if the reply asks for tool calls -> run them, append the results, go to 1
    3. otherwise the reply is the final answer -> stop

Step 3b builds exactly this loop as a LangGraph graph.

    python step3a_manual_loop.py "What are the common factors of change?"
    python step3a_manual_loop.py            # interactive
"""

import sys

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from llm import get_llm
from prompts import STUDY_ASSISTANT
from tools import TOOLS

# A stopping condition the harness enforces, so a confused model can't loop forever.
MAX_ITERATIONS = 5

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def run_agent(llm_with_tools, history: list) -> list:
    messages = [SystemMessage(STUDY_ASSISTANT)] + history

    for iteration in range(1, MAX_ITERATIONS + 1):
        # 1. Ask the model. bind_tools() sent it the tool names, descriptions and
        #    argument schemas, so it can reply with tool calls instead of text.
        reply = llm_with_tools.invoke(messages)
        messages.append(reply)
        usage = reply.usage_metadata or {}
        print(f"  [LLM call {iteration}: tokens in {usage.get('input_tokens', '?')}, "
              f"out {usage.get('output_tokens', '?')}]")

        # 3. No tool calls means the model has decided it can answer. Stop.
        if not reply.tool_calls:
            print(f"\nAI: {reply.content}")
            break

        # 2. Run every tool the model asked for and append each result as a
        #    ToolMessage. tool_call_id links the result to the request.
        for call in reply.tool_calls:
            print(f"  -> tool call: {call['name']}({call['args']})")
            result = TOOLS_BY_NAME[call["name"]].invoke(call["args"])
            print(f"  <- tool result: {len(result)} characters")
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
    else:
        print(f"\nStopped: no final answer after {MAX_ITERATIONS} LLM calls.")

    # Return the conversation without the system prompt, ready for the next turn.
    return messages[1:]


def main():
    llm_with_tools = get_llm().bind_tools(TOOLS)
    history = []
    if len(sys.argv) > 1:
        run_agent(llm_with_tools, [HumanMessage(" ".join(sys.argv[1:]))])
        return
    print("Ask the study assistant (blank line to quit).")
    while True:
        text = input("\nYou: ").strip()
        if not text:
            break
        history = run_agent(llm_with_tools, history + [HumanMessage(text)])


if __name__ == "__main__":
    main()
