"""CLI: `open-agent` command."""
from __future__ import annotations

import typer

from .agent import Agent

app = typer.Typer(help="open-agent — model-agnostic personal AI agent")


@app.command()
def chat(model: str = typer.Option("anthropic/claude-sonnet-4-6",
                                   "--model", "-m",
                                   help="LiteLLM model string, e.g. "
                                        "gemini/gemini-2.5-pro, gpt-4o, "
                                        "ollama/llama3.1, zhipu/glm-4-plus"),
          prompt: str = typer.Argument(..., help="What to ask the agent")):
    """Send one prompt to the agent and print the answer."""
    agent = Agent(model=model)
    typer.echo(agent.chat(prompt))


@app.command()
def models():
    """List a few well-known model strings (full list: 100+ via LiteLLM)."""
    for m in ["anthropic/claude-sonnet-4-6", "gemini/gemini-2.5-pro", "gpt-4o",
              "ollama/llama3.1", "zhipu/glm-4-plus"]:
        typer.echo(m)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
