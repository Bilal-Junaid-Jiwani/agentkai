"""agentkai: model-agnostic personal AI agent."""
from .agent import Agent, RunResult
from .events import Event, EventLog, list_runs, replay, summarize
from .permissions import Action, PermissionGate, cli_ask
from .providers import (
    ALIASES,
    LLMClient,
    LLMMessage,
    ProviderConfig,
    ToolCall,
    normalize_tool_calls,
    probe_capabilities,
    resolve_fallbacks,
    resolve_model,
)
from .scheduler import Job, Scheduler
from .subagents import (
    ChildRun,
    SubagentManager,
    default_manager,
    readonly_subset,
    scoped_registry,
)

__version__ = "0.4.0"

__all__ = [
    "Agent",
    "RunResult",
    "Event",
    "EventLog",
    "list_runs",
    "replay",
    "summarize",
    "Action",
    "PermissionGate",
    "cli_ask",
    "ALIASES",
    "LLMClient",
    "LLMMessage",
    "ProviderConfig",
    "ToolCall",
    "normalize_tool_calls",
    "probe_capabilities",
    "resolve_fallbacks",
    "resolve_model",
    "Job",
    "Scheduler",
    "ChildRun",
    "SubagentManager",
    "default_manager",
    "readonly_subset",
    "scoped_registry",
]
