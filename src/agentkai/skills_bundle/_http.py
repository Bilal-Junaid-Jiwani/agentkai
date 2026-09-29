"""Minimal stdlib HTTP client for skill tools.

No third-party dependencies: this keeps bundled skills installable
everywhere the agent runs. The transport is injectable so tests can run
with zero network access (pass ``transport=FakeTransport(...)``).

Security notes:
- Authorization headers are attached per-request but NEVER appear in error
  messages or logs — errors carry only status + a short body excerpt.
- HTTPS only is not enforced here (some local dev servers are http), but
  every bundled skill uses https base URLs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "agentkai/0.1 (+local agent)"


class HttpError(Exception):
    """A non-2xx HTTP response. ``status`` and a short ``body`` excerpt."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def _short(data: Any, limit: int = 200) -> str:
    """Extract a short human-readable excerpt from a JSON body."""
    if isinstance(data, dict):
        for key in ("message", "error", "error_description"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val[:limit]
            if isinstance(val, dict):
                msg = val.get("message")
                if isinstance(msg, str) and msg:
                    return msg[:limit]
        return json.dumps(data, ensure_ascii=False)[:limit]
    if isinstance(data, str):
        return data[:limit]
    return ""


class _UrllibTransport:
    """Default transport: stdlib urllib. Returns (status, headers, body)."""

    def request(self, method: str, url: str, headers: dict,
                body: bytes | None, timeout: float) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            # HTTPError is also the response object: read its body.
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            return exc.code, dict(exc.headers or {}), raw


class HttpClient:
    """Tiny JSON REST client.

    Args:
        base_url: e.g. "https://api.github.com".
        headers: default headers (e.g. Authorization) merged into every call.
        timeout: seconds per request.
        transport: object with ``request(method, url, headers, body, timeout)``
            returning ``(status, headers, body_bytes)``. Tests inject a fake.
    """

    def __init__(self, base_url: str, headers: dict | None = None,
                 timeout: float = 30,
                 transport: Any | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._transport = transport or _UrllibTransport()

    def request(self, method: str, path: str,
                params: dict | None = None,
                json_body: Any = None,
                raw_body: bytes | None = None,
                headers: dict | None = None) -> Any:
        """Perform a request. Returns parsed JSON (or text / None).

        Raises :class:`HttpError` on 4xx/5xx. Never leaks headers.
        """
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        merged = {"User-Agent": USER_AGENT, **self.headers, **(headers or {})}
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        status, _resp_headers, raw = self._transport.request(
            method.upper(), url, merged, body, self.timeout)
        data: Any = None
        if raw:
            text = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text
        if status >= 400:
            raise HttpError(status, _short(data))
        return data
