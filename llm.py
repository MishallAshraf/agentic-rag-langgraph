"""Shared access to the chat model, through OpenRouter.

OpenRouter is one API in front of many providers (Anthropic, OpenAI, Google,
open-weight models...). It speaks the OpenAI API format, so LangChain's
ChatOpenAI client works by pointing base_url at OpenRouter. Switching models is
then just a different model name - no code or package change.

Settings come from the .env file (see .env.example):
    OPENROUTER_API_KEY   required
    OPENROUTER_MODEL     optional, defaults to DEFAULT_MODEL
"""

import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"

load_dotenv()


def get_llm(temperature: float = 0) -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and put your key in it."
        )
    return ChatOpenAI(
        model=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL),
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        temperature=temperature,
        # Ask for token counts on streamed responses too, so we can show them.
        stream_usage=True,
    )
