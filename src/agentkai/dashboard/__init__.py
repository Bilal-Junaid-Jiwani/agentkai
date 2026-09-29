"""Local web dashboard (placeholder for Phase 7).

Target: FastAPI on 127.0.0.1 only, per-launch token auth, offline single-page
app, SSE stream over the agent event log — the same pattern as the okfsmith
dashboard. See research/ARCHITECTURE.md § dashboard.
"""
from __future__ import annotations

# Placeholder — full implementation lands in Phase 7.
DASHBOARD_PORT = 8931


def run(port: int = DASHBOARD_PORT) -> None:
    raise NotImplementedError(
        "Dashboard arrives in Phase 7 — see research/ARCHITECTURE.md")
