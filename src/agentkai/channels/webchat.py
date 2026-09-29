"""WebChat channel: a WebSocket server run by the gateway itself.

- Listens on 127.0.0.1 only (same localhost-only posture as the dashboard).
- The same port serves a minimal bundled HTML chat page (GET /) via the
  websocket handshake's ``process_request`` hook and the WS endpoint.
- JSON protocol:
    client -> server  {"type": "hello", "peer_id": "<stable id>"}
    client -> server  {"type": "msg", "text": "..."}
    server -> client  {"type": "msg", "text": "..."}
    server -> client  {"type": "typing", "on": true|false}
    server -> client  {"type": "error", "text": "..."}
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from pathlib import Path

from .core import Channel, InboundMessage

MAX_TEXT = 4000
STATIC_DIR = Path(__file__).parent / "static"


class WebChatChannel(Channel):
    """WebSocket chat served by the gateway (localhost only)."""

    name = "webchat"

    def __init__(self, port: int = 18790, host: str = "127.0.0.1") -> None:
        super().__init__()
        self.host = host
        self.port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._stop = threading.Event()
        self._connections: dict[str, object] = {}
        self._typing: set[str] = set()
        self._page = (STATIC_DIR / "chat.html").read_bytes()

    # -- Channel interface --------------------------------------------------

    def start(self, on_message) -> None:
        self.on_message = on_message
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="webchat-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(),
                                                   self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def send_text(self, peer_id: str, text: str) -> None:
        self._ws_send(peer_id, text)

    # -- server ---------------------------------------------------------------

    def _serve(self) -> None:
        import websockets
        from websockets.datastructures import Headers
        from websockets.http11 import Request, Response

        page = self._page
        outer = self

        def process_request(connection, request: Request):
            # A websocket handshake is also GET / — only serve the page for
            # plain HTTP requests; handshakes fall through.
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return None
            # Serve the bundled chat page on plain HTTP GET /; anything
            # else falls through to the websocket handshake.
            if request.method == "GET" and request.path in ("/", "/index.html"):
                return Response(
                    200, "OK",
                    Headers([("Content-Type", "text/html; charset=utf-8"),
                             ("Content-Length", str(len(page)))]),
                    page,
                )
            return None

        async def handler(ws) -> None:
            peer_id: str | None = None
            try:
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    kind = data.get("type")
                    if kind == "hello":
                        peer_id = str(data.get("peer_id") or
                                      f"web-{secrets.token_hex(4)}")
                        outer._connections[peer_id] = ws
                        await outer._emit(peer_id,
                                          {"type": "hello", "peer_id": peer_id})
                    elif kind == "msg" and peer_id:
                        outer.handle_client_text(peer_id,
                                                 str(data.get("text", "")))
                    elif kind == "ping" and peer_id:
                        await outer._emit(peer_id, {"type": "pong"})
            finally:
                if peer_id is not None:
                    outer._connections.pop(peer_id, None)
                    outer._typing.discard(peer_id)

        async def main() -> None:
            outer._server = await websockets.serve(
                handler, outer.host, outer.port,
                process_request=process_request)
            while not outer._stop.is_set():
                await asyncio.sleep(0.2)

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(main())
        except Exception:
            pass
        finally:
            pending = asyncio.all_tasks(self._loop)
            for t in pending:
                t.cancel()

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _emit(self, peer_id: str, payload: dict) -> None:
        ws = self._connections.get(peer_id)
        if ws is None:
            return
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            self._connections.pop(peer_id, None)

    def _emit_sync(self, peer_id: str, payload: dict) -> None:
        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(
            self._emit(peer_id, payload), self._loop)
        try:
            fut.result(timeout=10)
        except Exception:
            pass

    # -- inbound ---------------------------------------------------------------

    def handle_client_text(self, peer_id: str, text: str) -> None:
        """Route one client message. Public so tests can drive it directly."""
        text = (text or "").strip()
        if not text:
            return
        self._typing.add(peer_id)
        self._emit_sync(peer_id, {"type": "typing", "on": True})
        msg = InboundMessage(channel="webchat", peer_id=peer_id,
                             peer_name="web", text=text, timestamp=time.time())
        if self.on_message is not None:
            self.on_message(msg)

    # -- outbound ---------------------------------------------------------------

    def _ws_send(self, peer_id: str, text: str) -> None:
        if peer_id in self._typing:
            self._typing.discard(peer_id)
            self._emit_sync(peer_id, {"type": "typing", "on": False})
        self._emit_sync(peer_id, {"type": "msg", "text": text})
