"""Shared access to the chat model, through OpenRouter.

OpenRouter is one API in front of many providers (Anthropic, OpenAI, Google,
open-weight models...). It speaks the OpenAI API format, so LangChain's
ChatOpenAI client works by pointing base_url at OpenRouter. Switching models is
then just a different model name - no code or package change.

Settings come from the .env file (see .env.example):
    OPENROUTER_API_KEY   required
    OPENROUTER_MODEL     optional, defaults to DEFAULT_MODEL
    OPENROUTER_FALLBACK_MODELS  optional, comma-separated; "none" turns fallback off
"""

import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
MAX_OUTPUT_TOKENS = 2000
# How every component asks for structured output (graders, guards, checkers, agents).
# Not "function_calling": with it, the forced tool call was answered by the fallback model
# (gpt-4o-mini), not the configured one; OpenRouter switched quietly instead of failing. Found in Step 16, when the
# improvement loop showed the guard, grader and fact check had all been running on gpt-4o-mini.
STRUCTURED_METHOD = "json_schema"
# Tried in order if the main model fails (Step 9). OpenRouter already retries
# other PROVIDERS of the same model; this covers the model itself being down.
# A different vendor, so one company's outage cannot take out both.
DEFAULT_FALLBACK_MODELS = "openai/gpt-4o-mini"

load_dotenv()


def get_llm(temperature: float = 0, model: str | None = None) -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and put your key in it."
        )
    # An explicit model wins (the eval judge uses a stronger one), then .env, then the default.
    model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    fallbacks = os.getenv("OPENROUTER_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    if fallbacks.strip().lower() == "none":
        fallbacks = []
    else:
        fallbacks = [m.strip() for m in fallbacks.split(",") if m.strip() and m.strip() != model]
    return ChatOpenAI(
        model=model,
        # OpenRouter's "models" list: the first is tried first, the rest on failure.
        extra_body={"models": [model, *fallbacks]} if fallbacks else None,
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        temperature=temperature,
        # Without a timeout, a request the provider never answers blocks forever
        # (seen in Step 8: an evaluation run hung for over an hour). Fail after
        # 60 s and retry twice instead.
        timeout=60,
        max_retries=2,
        # A ceiling on each reply. Without it OpenRouter reserves credit for the
        # model's full output limit (64k tokens) on every call, and a low balance
        # refuses parallel calls (Step 9). The longest answers here are ~800 tokens.
        max_tokens=MAX_OUTPUT_TOKENS,
        # Ask for token counts on streamed responses too, so we can show them.
        stream_usage=True,
    )
