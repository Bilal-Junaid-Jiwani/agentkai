"""Local web dashboard: FastAPI on 127.0.0.1, per-launch token, offline SPA.

See server.py for the app and API.md for the API contract.
"""
from __future__ import annotations

import secrets

from .server import create_app

DASHBOARD_PORT = 8931
LOCAL_HOST = "127.0.0.1"


def run(port: int = DASHBOARD_PORT, host: str = LOCAL_HOST) -> None:
    """Launch the dashboard. Binds 127.0.0.1 ONLY — anything else raises.

    A fresh random token is generated per launch and printed exactly once
    as part of the dashboard URL. It is never logged: uvicorn's access log
    is disabled so the ``?token=`` query string cannot leak into logs.
    """
    if host != LOCAL_HOST:
        raise RuntimeError(
            f"refusing to bind {host!r}: the dashboard only serves "
            f"{LOCAL_HOST} (localhost-only is the entire v1 security model)")
    token = secrets.token_urlsafe(32)
    app = create_app(token)

    # Printed exactly once — the only place the token ever appears.
    print(f"\n  agentkai dashboard → http://{host}:{port}/?token={token}\n",
          flush=True)

    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning",
                access_log=False)
