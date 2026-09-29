"""Agent loop: reason -> act (tool calls) -> observe -> repeat.

Provider-agnostic: works with any model behind the Provider abstraction, as
long as it supports function calling (Claude, Gemini, GPT, and most others
do; small local models vary — see research/ARCHITECTURE.md).
"""
from __future__ import annotations

import json

from .providers import Provider
from .tools import default_registry


SYSTEM_PROMPT = """You are open-agent, a personal AI assistant running on the \
user's own machine. You have tools: use them to get things done instead of \
guessing. Be concise, honest about uncertainty, and never invent file contents, \
URLs, or results — read them with your tools first."""


class Agent:
    def __init__(self, model: str, system_prompt: str = SYSTEM_PROMPT,
                 max_steps: int = 25, **provider_kwargs):
        self.provider = Provider(model, **provider_kwargs)
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.registry = default_registry()
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]

    def _run_tool_calls(self, message) -> list[dict]:
        results = []
        for call in message.tool_calls or []:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            try:
                output = self.registry.get(name).run(**args)
            except Exception as e:  # noqa: BLE001
                output = {"error": f"tool {name} failed: {e}"}
            results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": json.dumps(output)[:8000],
            })
        return results

    def step(self, user_text: str | None = None) -> str:
        """Run the loop until the model answers without tool calls."""
        if user_text is not None:
            self.messages.append({"role": "user", "content": user_text})
        for _ in range(self.max_steps):
            resp = self.provider.chat(self.messages,
                                      tools=self.registry.schemas())
            msg = resp.choices[0].message
            self.messages.append(msg.model_dump())
            if not msg.tool_calls:
                return msg.content or ""
            self.messages.extend(self._run_tool_calls(msg))
        return "Stopped after max steps without a final answer."

    def chat(self, text: str) -> str:
        return self.step(text)
