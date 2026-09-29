# open-agent

An open-source personal AI agent. Model-agnostic by design: bring your own model —
Claude, Gemini, GPT, Ollama (local), GLM, or anything LiteLLM speaks — and the
agent keeps the same tools, memory, scheduler, and dashboard.

Inspired by the architecture of modern personal AI assistants: an agent loop over
a tool registry, markdown-file memory, background scheduling, MCP tool servers,
and a local web dashboard.

## Status

🚧 Early scaffold — the architecture research is in `research/` and the build
follows the phased plan in `research/ARCHITECTURE.md`.

## Quick start (scaffold)

```bash
pip install -e .
export ANTHROPIC_API_KEY=...   # or GOOGLE_API_KEY / OPENAI_API_KEY, etc.
open-agent --model anthropic/claude-sonnet-4-6 "hello"
```

## Layout

- `src/open_agent/` — agent loop, provider abstraction, tools, memory, scheduler, dashboard
- `docs/` — user documentation for every feature (original docs for this project)
- `research/` — architecture research and case studies
- `examples/` — example configs and skills

## License

Apache-2.0
