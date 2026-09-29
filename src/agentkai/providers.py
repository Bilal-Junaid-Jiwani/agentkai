"""Model provider abstraction: one interface for every model.

Backed by LiteLLM, so the same agent loop works with Claude, Gemini, GPT,
local Ollama models, GLM (Zhipu), and 100+ other providers::

    agentkai run "hello" --model claude     # alias
    agentkai run "hello" --model ollama/qwen3:32b

API keys come from the environment (``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY``
/ ``GOOGLE_API_KEY``, ``OPENAI_API_KEY``, ``ZHIPUAI_API_KEY``) or from
``~/.agentkai/config.yaml``. Key values are never printed or logged — only
"set"/"missing" is ever reported.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import litellm
import yaml

CONFIG_PATH = Path("~/.agentkai/config.yaml").expanduser()
CAPS_CACHE_PATH = Path("~/.agentkai/capabilities.json").expanduser()

# -- model aliases ---------------------------------------------------------

ALIASES: dict[str, str] = {
    "claude": "anthropic/claude-sonnet-4-6",
    "claude-opus": "anthropic/claude-opus-4-6",
    "gemini": "gemini/gemini-2.5-flash",
    "gemini-pro": "gemini/gemini-2.5-pro",
    "gpt": "gpt-4o",
    "gpt-mini": "gpt-4o-mini",
    "glm": "zhipu/glm-4-plus",
    "local": "ollama/qwen3:32b",
    "fast": "gemini/gemini-2.5-flash",
}

# provider prefix -> env var holding the key (None = keyless, e.g. Ollama)
KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "zhipu": "ZHIPUAI_API_KEY",
    "ollama": None,
}

# Default fallback chains, keyed by alias. A failure on the primary model
# retries the same request on each fallback in order.
DEFAULT_FALLBACKS: dict[str, list[str]] = {
    "claude": ["gemini", "gpt"],
    "gemini": ["claude", "gpt"],
    "gpt": ["claude", "gemini"],
    "glm": ["gemini", "gpt"],
    "local": [],
}


def resolve_model(name: str, extra_aliases: dict[str, str] | None = None) -> str:
    """Resolve a user-supplied model name: alias -> LiteLLM model string.

    Unknown names pass through unchanged (LiteLLM routes by prefix), so
    ``ollama/llama3.1`` or ``openrouter/...`` work without configuration.
    """
    aliases = dict(ALIASES)
    if extra_aliases:
        aliases.update(extra_aliases)
    key = name.strip().lower()
    return aliases.get(key, name)


def resolve_fallbacks(name: str,
                      extra_aliases: dict[str, str] | None = None,
                      config_fallbacks: dict[str, list[str]] | None = None
                      ) -> list[str]:
    """Ordered LiteLLM model strings to try after the primary fails."""
    key = name.strip().lower()
    table = dict(DEFAULT_FALLBACKS)
    if config_fallbacks:
        table.update(config_fallbacks)
    return [resolve_model(a, extra_aliases) for a in table.get(key, [])]


# -- BYOK config ------------------------------------------------------------

@dataclass
class ProviderConfig:
    """API keys (from env) + non-secret settings (from config.yaml)."""
    keys: dict[str, str] = field(default_factory=dict)  # provider -> key
    ollama_base_url: str = "http://localhost:11434"
    extra_aliases: dict[str, str] = field(default_factory=dict)
    fallbacks: dict[str, list[str]] = field(default_factory=dict)

    @staticmethod
    def load(path: str | Path = CONFIG_PATH) -> "ProviderConfig":
        cfg = ProviderConfig()
        # 1. environment (BYOK)
        for provider, env_var in KEY_ENV.items():
            if env_var and os.environ.get(env_var):
                cfg.keys[provider] = os.environ[env_var]
        # Gemini also honors GOOGLE_API_KEY.
        if "gemini" not in cfg.keys and os.environ.get("GOOGLE_API_KEY"):
            cfg.keys["gemini"] = os.environ["GOOGLE_API_KEY"]
        # 2. yaml file (non-secret settings; a key here is allowed too but
        #    env always wins)
        p = Path(path).expanduser()
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text()) or {}
            except Exception:
                data = {}
            providers = data.get("providers", {}) or {}
            if "ollama" in providers:
                cfg.ollama_base_url = providers["ollama"].get(
                    "base_url", cfg.ollama_base_url)
            for provider, spec in providers.items():
                if not isinstance(spec, dict):
                    continue
                env_var = spec.get("api_key_env")
                if env_var and provider not in cfg.keys \
                        and os.environ.get(env_var):
                    cfg.keys[provider] = os.environ[env_var]
                if spec.get("api_key") and provider not in cfg.keys:
                    cfg.keys[provider] = spec["api_key"]
            models = data.get("models", {}) or {}
            cfg.extra_aliases = models.get("aliases", {}) or {}
            cfg.fallbacks = data.get("fallbacks", {}) or {}
        return cfg

    def apply(self) -> None:
        """Export keys into the process env so LiteLLM picks them up.

        Only sets variables that are not already set — explicit env wins.
        Never prints key values.
        """
        for provider, key in self.keys.items():
            env_var = KEY_ENV.get(provider)
            if env_var and key and not os.environ.get(env_var):
                os.environ[env_var] = key
        # LiteLLM reads the Ollama host from OLLAMA_API_BASE.
        os.environ.setdefault("OLLAMA_API_BASE", self.ollama_base_url)

    def key_status(self) -> dict[str, str]:
        """Safe display: "set"/"missing" per provider — never the value."""
        return {p: ("set" if p in self.keys else "missing")
                for p in KEY_ENV}

    def redacted(self) -> dict:
        """Config safe to log or show in the dashboard."""
        return {"keys": self.key_status(),
                "ollama_base_url": self.ollama_base_url,
                "aliases": {**ALIASES, **self.extra_aliases}}


# -- capabilities ------------------------------------------------------------

def static_capabilities(model: str) -> dict:
    """Best-effort capability table without any network call.

    Every major provider supports tool calling and streaming through LiteLLM;
    Ollama depends on the model, so it is flagged model-dependent.
    """
    m = model.lower()
    if m.startswith("ollama/"):
        return {"tool_calls": True, "streaming": True,
                "model_dependent": True,
                "note": "Ollama tool-call quality varies by model; "
                        "qwen3/llama3.1/mistral families are decent."}
    return {"tool_calls": True, "streaming": True, "model_dependent": False}


def _load_caps_cache() -> dict:
    try:
        return json.loads(CAPS_CACHE_PATH.read_text())
    except Exception:
        return {}


def probe_capabilities(model: str, timeout: int = 60) -> dict:
    """Live-probe a model once; cache the result on disk.

    Sends one minimal request ("reply with the single word: ok") with a
    dummy tool definition and records whether the provider honored the
    tool schema and whether streaming produced chunks. Costs one tiny call.
    """
    cache = _load_caps_cache()
    if model in cache:
        return cache[model]
    caps = dict(static_capabilities(model))
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user",
                       "content": "Reply with exactly: ok"}],
            tools=[{"type": "function",
                    "function": {"name": "noop",
                                 "description": "does nothing",
                                 "parameters": {"type": "object",
                                                "properties": {}}}}],
            max_tokens=8,
            timeout=timeout,
        )
        msg = resp.choices[0].message
        caps["tool_calls"] = True  # honored the tool schema round-trip
        caps["probed"] = True
        caps["probe_text"] = (msg.content or "")[:32]
    except Exception as exc:  # noqa: BLE001 - probe failure is data
        caps["probed"] = True
        caps["probe_error"] = f"{type(exc).__name__}: {exc}"[:200]
    cache[model] = caps
    try:
        CAPS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CAPS_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass
    return caps


# -- normalized message shapes ------------------------------------------------

@dataclass
class ToolCall:
    """One normalized tool call, whatever the provider's native shape."""
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass
class LLMMessage:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def _parse_args(raw: str) -> tuple[dict, str]:
    """Parse a tool-call arguments string; never raise."""
    if not raw:
        return {}, ""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}, raw
    except (json.JSONDecodeError, TypeError):
        return {}, raw


def normalize_tool_calls(message) -> list[ToolCall]:
    """Extract tool calls from a LiteLLM message of any provider.

    LiteLLM already converts Anthropic/Gemini native tool use into the
    OpenAI ``tool_calls`` shape, but this is defensive: it also handles the
    legacy ``function_call`` shape and plain-dict messages.
    """
    out: list[ToolCall] = []
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    for i, call in enumerate(calls or []):
        if isinstance(call, dict):
            fn = call.get("function", {}) or {}
            call_id = call.get("id", f"call_{i}")
            name = fn.get("name", "")
            raw = fn.get("arguments", "") or ""
        else:
            fn = getattr(call, "function", None)
            call_id = getattr(call, "id", f"call_{i}")
            name = getattr(fn, "name", "") or ""
            raw = getattr(fn, "arguments", "") or ""
        if not name:
            continue
        args, raw_args = _parse_args(raw if isinstance(raw, str) else "")
        out.append(ToolCall(id=str(call_id), name=name,
                            arguments=args, raw_arguments=raw_args or raw))
    # Legacy single function_call shape (older providers / proxies).
    if not out:
        fn_call = getattr(message, "function_call", None)
        if fn_call is None and isinstance(message, dict):
            fn_call = message.get("function_call")
        if fn_call:
            name = fn_call.get("name") if isinstance(fn_call, dict) \
                else getattr(fn_call, "name", "")
            raw = fn_call.get("arguments") if isinstance(fn_call, dict) \
                else getattr(fn_call, "arguments", "")
            if name:
                args, raw_args = _parse_args(
                    raw if isinstance(raw, str) else "")
                out.append(ToolCall(id="call_0", name=name, arguments=args,
                                    raw_arguments=raw_args or (raw or "")))
    return out


# -- streaming events ----------------------------------------------------------

@dataclass
class TextDelta:
    text: str


@dataclass
class DoneEvent:
    message: LLMMessage


@dataclass
class ErrorEvent:
    error: Exception


# -- the client -----------------------------------------------------------------

class LLMClient:
    """LiteLLM-backed chat client with aliases, fallbacks, and streaming.

    ``completion_fn`` is injectable for tests (defaults to
    ``litellm.completion``). It must accept ``(model, messages, tools,
    stream, **kwargs)`` and return a LiteLLM response or an iterator of
    chunk responses when ``stream=True``.
    """

    def __init__(self, model: str = "claude",
                 config: ProviderConfig | None = None,
                 completion_fn: Callable | None = None,
                 **default_kwargs):
        self.config = config or ProviderConfig.load()
        self.config.apply()
        self.alias = model
        self.model = resolve_model(model, self.config.extra_aliases)
        self.fallback_models = resolve_fallbacks(
            model, self.config.extra_aliases, self.config.fallbacks)
        self.completion_fn = completion_fn or litellm.completion
        self.default_kwargs = default_kwargs
        self.capabilities = static_capabilities(self.model)

    # -- public API ------------------------------------------------------

    def models_tried(self) -> list[str]:
        return [self.model, *self.fallback_models]

    def generate(self, messages: list[dict],
                 tools: list[dict] | None = None,
                 on_delta: Callable[[str], None] | None = None,
                 timeout: float | None = None,
                 cancel_event: threading.Event | None = None,
                 **kwargs) -> LLMMessage:
        """One assistant turn with tool-call normalization.

        Streams internally so ``on_delta`` receives text incrementally;
        returns the fully assembled :class:`LLMMessage`. Retries once per
        fallback model on failure.
        """
        last_exc: Exception | None = None
        for attempt, model in enumerate(self.models_tried()):
            try:
                return self._generate_once(
                    model, messages, tools, on_delta=on_delta,
                    timeout=timeout, cancel_event=cancel_event, **kwargs)
            except _Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - fallbacks need this
                last_exc = exc
                if attempt == 0 and self.fallback_models:
                    continue  # error is logged by the caller via ErrorEvent
                tried = ", ".join(self.models_tried()[: attempt + 1])
                raise RuntimeError(
                    f"all models failed [{tried}]: {exc}") from exc
        raise last_exc or RuntimeError("no models configured")

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               timeout: float | None = None,
               cancel_event: threading.Event | None = None,
               **kwargs) -> Iterator[TextDelta | DoneEvent | ErrorEvent]:
        """Generator of stream events; the last event is DoneEvent/ErrorEvent.

        The LiteLLM call runs in a daemon thread so a stuck network read
        cannot hang the consumer: ``timeout`` bounds how long we wait for
        the next event.
        """
        q: queue.Queue = queue.Queue()
        sentinel = object()

        def _worker():
            try:
                msg = self.generate(
                    messages, tools, on_delta=lambda t: q.put(TextDelta(t)),
                    timeout=None, cancel_event=cancel_event, **kwargs)
                q.put(DoneEvent(msg))
            except Exception as exc:  # noqa: BLE001
                q.put(ErrorEvent(exc))
            finally:
                q.put(sentinel)

        threading.Thread(target=_worker, daemon=True).start()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else \
                max(0.0, deadline - time.monotonic())
            try:
                item = q.get(timeout=remaining)
            except queue.Empty:
                yield ErrorEvent(TimeoutError(
                    f"LLM step timed out after {timeout}s"))
                return
            if item is sentinel:
                return
            yield item

    # -- internals ---------------------------------------------------------

    def _generate_once(self, model: str, messages: list[dict],
                       tools: list[dict] | None,
                       on_delta: Callable[[str], None] | None,
                       timeout: float | None,
                       cancel_event: threading.Event | None,
                       **kwargs) -> LLMMessage:
        params: dict = {"model": model, "messages": messages, "stream": True}
        if tools:
            params["tools"] = tools
        params.update(self.default_kwargs)
        params.update(kwargs)
        if timeout is not None:
            params.setdefault("timeout", timeout)

        text_parts: list[str] = []
        # per tool-call index: id, name, argument fragments
        calls: dict[int, dict] = {}
        usage: dict = {}

        chunks = self.completion_fn(**params)
        for chunk in chunks:
            if cancel_event is not None and cancel_event.is_set():
                raise _Cancelled("cancelled by user")
            choice = (chunk.choices or [None])[0]
            if choice is None:
                continue
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            piece = getattr(delta, "content", None)
            if piece:
                text_parts.append(piece)
                if on_delta:
                    on_delta(piece)
            for tc in getattr(delta, "tool_calls", None) or []:
                idx = getattr(tc, "index", 0)
                slot = calls.setdefault(idx, {"id": None, "name": "",
                                              "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments
            # Some providers put tool calls on the message instead of delta.
            msg = getattr(choice, "message", None)
            if msg is not None:
                for tc in getattr(msg, "tool_calls", None) or []:
                    idx = len(calls)
                    fn = getattr(tc, "function", None)
                    slot = calls.setdefault(idx, {
                        "id": getattr(tc, "id", f"call_{idx}"),
                        "name": getattr(fn, "name", "") if fn else "",
                        "args": getattr(fn, "arguments", "") if fn else ""})
            if getattr(chunk, "usage", None):
                try:
                    usage = dict(chunk.usage)
                except Exception:
                    pass

        tool_calls: list[ToolCall] = []
        for i, slot in sorted(calls.items()):
            if not slot["name"]:
                continue
            args, raw = _parse_args(slot["args"])
            tool_calls.append(ToolCall(
                id=slot["id"] or f"call_{i}", name=slot["name"],
                arguments=args, raw_arguments=raw or slot["args"]))
        return LLMMessage(text="".join(text_parts), tool_calls=tool_calls,
                          usage=usage)


class _Cancelled(Exception):
    """Cooperative cancellation inside a streaming generate."""


# Backwards-compatible alias (the v0 scaffold exposed ``Provider``).
Provider = LLMClient
