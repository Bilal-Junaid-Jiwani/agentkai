# Privacy

- Runs on **your PC**. Tools, memory, scheduler, dashboard are local.
- The only data that leaves the PC is what you send to the model provider
  you chose (Anthropic/Google/OpenAI/...) — or nothing at all with local
  Ollama models.
- API keys live in environment variables or `~/.open-agent/config.yaml`,
  never in the repo or logs.
- The dashboard binds to `127.0.0.1` only.
