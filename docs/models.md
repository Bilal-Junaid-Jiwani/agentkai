# Models

open-agent is model-agnostic. The provider layer is LiteLLM, so one agent
codebase works with 100+ models. The user picks the model; everything else
(tools, memory, scheduler, dashboard) stays the same.

## Supported (verified via LiteLLM)

| Family | Example model string | Key env var |
|---|---|---|
| Claude (Anthropic) | `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| Gemini (Google) | `gemini/gemini-2.5-pro` | `GOOGLE_API_KEY` |
| GPT (OpenAI) | `gpt-4o` | `OPENAI_API_KEY` |
| Ollama (local) | `ollama/llama3.1` | none (runs on your PC) |
| GLM (Zhipu) | `zhipu/glm-4-plus` | `ZHIPU_API_KEY` |

## Switch models

```bash
open-agent --model gemini/gemini-2.5-pro "summarize this folder"
open-agent --model ollama/llama3.1 "draft a reply"
```

## Notes

- Tool-calling quality varies by model: frontier models (Claude/Gemini/GPT)
  are reliable; small local models may need simpler tool schemas.
- Aliases and fallbacks can be configured in `~/.open-agent/config.yaml`
  (planned).
