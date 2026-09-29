"""Model provider abstraction.

One interface for every model. Backed by LiteLLM, so the same agent loop works
with Claude, Gemini, GPT, local Ollama models, GLM, and 100+ other providers —
the user just names the model, e.g.::

    anthropic/claude-sonnet-4-6
    gemini/gemini-2.5-pro
    gpt-4o
    ollama/llama3.1
    zhipu/glm-4-plus

API keys come from the environment (ANTHROPIC_API_KEY, GOOGLE_API_KEY,
OPENAI_API_KEY, ...) or from ``~/.agentkai/config.yaml``.
"""
from __future__ import annotations

import litellm


class Provider:
    """Thin wrapper around litellm.completion with a stable message format."""

    def __init__(self, model: str, **kwargs):
        self.model = model
        self.kwargs = kwargs

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             stream: bool = False, **kwargs):
        """Send messages, optionally with tool definitions. Returns the raw
        litellm response (provider-agnostic Message object)."""
        params: dict = {"model": self.model, "messages": messages,
                        "stream": stream}
        if tools:
            params["tools"] = tools
        params.update(self.kwargs)
        params.update(kwargs)
        return litellm.completion(**params)

    @staticmethod
    def supported_models() -> list[str]:
        """All model strings litellm knows about (100+ providers)."""
        return litellm.model_list
