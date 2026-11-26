"""A fake LLM server for load tests: speaks the OpenAI chat API, costs nothing.

Load tests send hundreds of turns. Through a real model that would cost money and,
worse, the model's own speed would hide what we want to measure: how the
platform (API, database, cache, queue) behaves under load. This server answers
like a model, with a fixed, configurable delay, so every run is cheap and
repeatable.

    python loadtest/mock_llm.py                    # listens on http://127.0.0.1:8900
    set LLM_BASE_URL=http://127.0.0.1:8900/v1      # then start the API with this set

How it answers (just enough for a whole turn to run through the real graph):
  - a structured-output request (response_format json_schema): valid JSON built
    from the schema, with values that let the turn pass (category "ok",
    sufficient/supported true, the first passage relevant);
  - a request with tools and no tool result yet: a call to the first tool, with
    the user's message as every text argument (so retrieval really runs);
  - anything else: a short answer that cites passage [1], streamed token by token.

Delays (illustrative, roughly a small hosted model):
  MOCK_TTFT_MS   time to the first token, default 400
  MOCK_TOKEN_MS  time per further token, default 15
"""

import asyncio
import json
import os
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TTFT_S = int(os.getenv("MOCK_TTFT_MS", "400")) / 1000
TOKEN_S = int(os.getenv("MOCK_TOKEN_MS", "15")) / 1000
ANSWER = ("This is a mock answer for load testing. The passage explains the idea step by step, "
          "and the key point is stated clearly in the source [1].")

app = FastAPI()
stats = {"requests": 0}


# ------------------------------------------------------------------ building a reply

def value_for(schema: dict, defs: dict, name: str, user_text: str):
    """A valid value for one JSON-schema node, chosen so a turn passes every check."""
    if "$ref" in schema:
        return value_for(defs[schema["$ref"].split("/")[-1]], defs, name, user_text)
    if "anyOf" in schema:
        return value_for(schema["anyOf"][0], defs, name, user_text)
    if "enum" in schema:
        return "ok" if "ok" in schema["enum"] else schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {key: value_for(sub, defs, key, user_text) for key, sub in props.items()}
    if kind == "array":
        item = schema.get("items", {})
        return [1] if item.get("type") == "integer" else []
    if kind == "boolean":
        return True
    if kind in ("integer", "number"):
        return 1
    # Strings: fields that should be empty when all is well stay empty.
    if name in ("next_query", "problems"):
        return ""
    return user_text[:200] if name in ("query", "question") else "ok"


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return content or ""
    return ""


def plan_reply(body: dict) -> dict:
    """Decide what to answer: {"content": str} or {"tool_call": {...}}."""
    messages = body.get("messages", [])
    user_text = last_user_text(messages)
    fmt = body.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        schema = fmt["json_schema"]["schema"]
        value = value_for(schema, schema.get("$defs", {}), "", user_text)
        return {"content": json.dumps(value)}
    tools = body.get("tools") or []
    answered_by_tool = any(m.get("role") == "tool" for m in messages[-3:])
    if tools and not answered_by_tool:
        fn = tools[0]["function"]
        params = fn.get("parameters", {})
        args = value_for(params, params.get("$defs", {}), "", user_text)
        args = {k: (user_text[:200] if isinstance(v, str) else v) for k, v in args.items()}
        return {"tool_call": {"id": "call_" + uuid.uuid4().hex[:12], "name": fn["name"],
                              "arguments": json.dumps(args)}}
    return {"content": ANSWER}


def usage(text: str) -> dict:
    out = max(1, len(text) // 4)
    return {"prompt_tokens": 500, "completion_tokens": out, "total_tokens": 500 + out}


# ------------------------------------------------------------------ the endpoint

@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    stats["requests"] += 1
    reply = plan_reply(body)
    model = body.get("model", "mock")
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:12], int(time.time())
    text = reply.get("content", "")
    call = reply.get("tool_call")

    if not body.get("stream"):
        await asyncio.sleep(TTFT_S + TOKEN_S * len(text.split()))
        message = {"role": "assistant", "content": text if not call else None}
        if call:
            message["tool_calls"] = [{"id": call["id"], "type": "function",
                                      "function": {"name": call["name"], "arguments": call["arguments"]}}]
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": model,
                             "choices": [{"index": 0, "message": message,
                                          "finish_reason": "tool_calls" if call else "stop"}],
                             "usage": usage(text or call["arguments"])})

    async def events():
        def chunk(delta: dict, finish=None) -> str:
            return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                          "model": model, "choices": [{"index": 0, "delta": delta,
                                                                       "finish_reason": finish}]}) + "\n\n"
        await asyncio.sleep(TTFT_S)
        if call:
            yield chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                                              "function": {"name": call["name"],
                                                                           "arguments": call["arguments"]}}]})
            yield chunk({}, "tool_calls")
        else:
            yield chunk({"role": "assistant", "content": ""})
            for i, word in enumerate(text.split(" ")):
                if i:
                    await asyncio.sleep(TOKEN_S)
                yield chunk({"content": word if i == 0 else " " + word})
            yield chunk({}, "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                         "model": model, "choices": [],
                                         "usage": usage(text or call["arguments"])}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/stats")
async def get_stats():
    return stats


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("MOCK_PORT", "8900")), log_level="warning")
