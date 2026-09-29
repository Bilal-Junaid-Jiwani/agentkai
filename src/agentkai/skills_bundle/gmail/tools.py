"""Gmail skill tools: search, read thread, draft, send (Gmail REST API)."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from agentkai.skills_bundle._common import (
    auth_error, get_token, test_transport,
)
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

ENV_VAR = "GMAIL_TOKEN"
TOKEN_FILE = "gmail_token.json"
BASE = "https://gmail.googleapis.com/gmail/v1"


def _client(config: dict | None) -> HttpClient | None:
    token = get_token(config, ENV_VAR, TOKEN_FILE)
    if not token:
        return None
    return HttpClient(BASE, headers={"Authorization": f"Bearer {token}"},
                      transport=test_transport(config))


def _no_auth() -> str:
    return auth_error("gmail", ENV_VAR, TOKEN_FILE)


def _decode_body(data: str) -> str:
    """base64url -> text, forgiving."""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _part_text(part: dict) -> str:
    """Prefer text/plain, fall back to first decodable part."""
    mime = part.get("mimeType", "")
    body = part.get("body", {}) or {}
    data = body.get("data", "")
    if mime == "text/plain" and data:
        return _decode_body(data)
    for sub in part.get("parts", []) or []:
        text = _part_text(sub)
        if text:
            return text
    if data and mime.startswith("text/"):
        return _decode_body(data)
    return ""


def _summarize_message(msg: dict) -> dict:
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in (msg.get("payload", {}) or {}).get("headers", [])}
    return {
        "id": msg.get("id"),
        "threadId": msg.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "body": _part_text(msg.get("payload", {}) or {})[:4000],
    }


def _rfc822(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def get_tools(config: dict | None = None) -> list[Tool]:
    def gmail_search(query: str, max_results: int = 10) -> dict | str:
        """Search Gmail (Gmail query syntax). Returns message/thread IDs."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request("GET", "/users/me/messages", params={
                "q": query, "maxResults": min(max(int(max_results), 1), 50)})
        except HttpError as exc:
            return f"ERROR: gmail search failed: {exc}"
        msgs = (data or {}).get("messages", []) or []
        return {
            "messages": [{"id": m.get("id"), "threadId": m.get("threadId")}
                         for m in msgs],
            "resultSizeEstimate": (data or {}).get("resultSizeEstimate", 0),
        }

    def gmail_read_thread(thread_id: str) -> dict | str:
        """Read a full Gmail thread (headers, snippet, plain-text bodies)."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request(
                "GET", f"/users/me/threads/{thread_id}",
                params={"format": "full"})
        except HttpError as exc:
            return f"ERROR: gmail read thread failed: {exc}"
        messages = (data or {}).get("messages", []) or []
        return {"id": (data or {}).get("id"),
                "messages": [_summarize_message(m) for m in messages]}

    def gmail_create_draft(to: str, subject: str, body: str) -> dict | str:
        """Create (not send) a Gmail draft. Returns the draft ID."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request("POST", "/users/me/drafts",
                                  json_body={"message": {
                                      "raw": _rfc822(to, subject, body)}})
        except HttpError as exc:
            return f"ERROR: gmail create draft failed: {exc}"
        return {"draft_id": (data or {}).get("id"),
                "message_id": ((data or {}).get("message") or {}).get("id")}

    def gmail_send(to: str, subject: str, body: str) -> dict | str:
        """Send an email immediately. HIGH RISK — gated, prefer drafts."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request("POST", "/users/me/messages/send",
                                  json_body={"raw": _rfc822(to, subject, body)})
        except HttpError as exc:
            return f"ERROR: gmail send failed: {exc}"
        return {"message_id": (data or {}).get("id"),
                "thread_id": (data or {}).get("threadId")}

    return [
        Tool(
            name="gmail_search",
            description=("Search the user's Gmail with Gmail query syntax "
                         "(from:, is:unread, after:YYYY/MM/DD …). Returns "
                         "message and thread IDs; use gmail_read_thread to "
                         "read one."),
            json_schema={"type": "object",
                         "properties": {
                             "query": {"type": "string"},
                             "max_results": {"type": "integer"}},
                         "required": ["query"]},
            risk="low",
            func=gmail_search,
        ),
        Tool(
            name="gmail_read_thread",
            description=("Read a full Gmail thread: headers, snippet and "
                         "plain-text bodies of every message."),
            json_schema={"type": "object",
                         "properties": {"thread_id": {"type": "string"}},
                         "required": ["thread_id"]},
            risk="low",
            func=gmail_read_thread,
        ),
        Tool(
            name="gmail_create_draft",
            description=("Create a Gmail draft (does NOT send). Returns the "
                         "draft ID for review."),
            json_schema={"type": "object",
                         "properties": {
                             "to": {"type": "string"},
                             "subject": {"type": "string"},
                             "body": {"type": "string"}},
                         "required": ["to", "subject", "body"]},
            risk="medium",
            func=gmail_create_draft,
        ),
        Tool(
            name="gmail_send",
            description=("Send an email immediately via Gmail. HIGH RISK: "
                         "delivers real mail. Only send to recipients the "
                         "user explicitly named; prefer gmail_create_draft "
                         "when unsure."),
            json_schema={"type": "object",
                         "properties": {
                             "to": {"type": "string"},
                             "subject": {"type": "string"},
                             "body": {"type": "string"}},
                         "required": ["to", "subject", "body"]},
            risk="high",
            func=gmail_send,
        ),
    ]


__all__ = ["get_tools", "ENV_VAR", "TOKEN_FILE"]
