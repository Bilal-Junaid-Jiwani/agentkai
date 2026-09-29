# Models

AgentKai is model-agnostic. The provider layer is LiteLLM, so one agent
codebase works with 100+ models. You pick the model per run, per job, or
per channel; everything else (tools, memory, scheduler, dashboard) stays
the same.

## Aliases

Short names so you don't memorize provider strings. Run
`agentkai models` to see the live table (including your own custom aliases).

| Alias | Resolves to | Key |
|---|---|---|
| `claude` | `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `claude-opus` | `anthropic/claude-opus-4-6` | `ANTHROPIC_API_KEY` |
| `gemini` | `gemini/gemini-2.5-flash` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) |
| `gemini-pro` | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` |
| `gpt` | `gpt-4o` | `OPENAI_API_KEY` |
| `gpt-mini` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `glm` | `zhipu/glm-4-plus` | `ZHIPUAI_API_KEY` |
| `local` | `ollama/qwen3:32b` | none (your PC) |
| `fast` | `gemini/gemini-2.5-flash` | `GEMINI_API_KEY` |

Anything that isn't an alias passes through to LiteLLM unchanged, so
`agentkai run --model "anthropic/claude-sonnet-4-6"` works too.

## Key setup (BYOK)

Keys come from the environment first — this is the recommended way:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=...
export OPENAI_API_KEY=...
export ZHIPUAI_API_KEY=...
```

Alternatively, `~/.agentkai/config.yaml` holds non-secret settings plus
optional key configuration:

```yaml
providers:
  ollama:
    base_url: http://localhost:11434   # default; change for remote Ollama
models:
  aliases:
    workhorse: anthropic/claude-sonnet-4-6   # your own aliases
```

Environment always wins over the file. Keys are **never printed or logged** —
`agentkai models` shows only `set` / `missing` per provider. See
[privacy.md](privacy.md).

## Fallbacks

Each alias has a fallback chain: if the primary model errors (rate limit,
outage, bad key), the request retries on the next model in the chain
automatically. The chain is defined per alias in the provider module; your
custom aliases in `config.yaml` can set their own.

## Capabilities

Before trusting a model with tool-heavy work, AgentKai can probe it:
one tiny request ("reply with the single word: ok") checks that tool
calling and streaming actually work, and the result is cached on disk.
The agent loop also normalizes tool calls across providers — Anthropic
native calls and OpenAI-style function calls arrive at your tools in the
same shape — so tools never care which model is behind them.

Small local models vary: some Ollama models handle tools well, some don't.
If a local model ignores tool calls, try a larger one or switch aliases.
`agentkai run` fails cleanly (exit 1, readable error) when the provider is
unreachable — e.g. `--model local` without Ollama running — instead of
hanging.

## Choosing

- **Best reasoning / default:** `claude`
- **Cheap + fast:** `gemini` (flash) or `gpt-mini`
- **Private / offline:** `local` (needs Ollama running; quality depends on the model you pulled)
- **China-region APIs:** `glm`

Cost and data handling are between you and the provider: hosted models send
your prompts to that provider's API. If that matters, use `local`.
