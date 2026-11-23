# 0005. OpenRouter as the LLM provider

- Status: Accepted
- Date: 2026-11-23
- Step: 2

## Context

From Step 2 the system needs an LLM, and from Step 3 that LLM must make reliable tool calls: choosing the right tool and producing valid arguments. No provider API key was configured. The machine has 8 GB RAM and no GPU. Ollama model files for `phi4-mini` exist in `D:\Ollama\models`, but Ollama is not on the PATH.

## Decision

Use OpenRouter, accessed through LangChain's `ChatOpenAI` client with `base_url` set to `https://openrouter.ai/api/v1` (OpenRouter speaks the OpenAI API format). The model is created in one place, `get_llm()` in `llm.py`. The key and model are read from `.env`; the default model is `anthropic/claude-haiku-5.5`, chosen for reliable tool calling at low cost (USD 0.10 per million input tokens and 0.50 per million output tokens at the time of writing).

## Alternatives considered

- **A provider's own API directly (for example Anthropic):** one less intermediary, and access to provider-specific features first. But each provider needs its own account, key and client package, which makes comparing models harder.
- **A free hosted tier (for example Groq or Gemini):** no cost, but tied to one provider's models and tighter rate limits.
- **Local Ollama with `phi4-mini`:** free and offline, but slow on this machine and unreliable at tool calls.

## Consequences

- One key gives access to hundreds of models from many providers. Changing model is an edit to `OPENROUTER_MODEL`, which makes the course's model-selection topic easy to explore: the same agent can be run on a small and a large model and compared.
- Costs money per token; usage is shown after each reply in Step 2 so it stays visible.
- OpenRouter sits between the app and the provider: one more service that can fail or add latency, and prompts pass through it. Provider-specific features may arrive later or not at all through the OpenAI-compatible format.
- Because everything goes through `get_llm()`, a fallback to a second model or provider (Step 9) is a contained change.
- The key lives in `.env`, which is listed in `.gitignore` and must never be shared.
