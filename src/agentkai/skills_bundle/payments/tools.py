"""Payments skill tools (Stripe payment intents).

All tools are HIGH RISK: creating an intent prepares a charge and every
call goes through the permission gate. Keys are never logged.
"""
from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any

from agentkai.skills_bundle._common import (
    auth_error, get_token, home_dir, test_transport,
)
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

ENV_VAR = "STRIPE_SECRET_KEY"
TOKEN_FILE = "stripe.json"
BASE = "https://api.stripe.com"

_PI_ID = re.compile(r"^pi_[A-Za-z0-9]+$")


def _client(config: dict | None) -> HttpClient | None:
    secret = get_token(config, ENV_VAR, TOKEN_FILE)
    if not secret:
        return None
    # Stripe authenticates with the secret key as the Basic-auth username.
    basic = base64.b64encode(f"{secret}:".encode()).decode()
    return HttpClient(BASE,
                      headers={"Authorization": f"Basic {basic}"},
                      transport=test_transport(config))


def _no_auth() -> str:
    return auth_error("payments", ENV_VAR, TOKEN_FILE)


def _form(data: dict) -> tuple[bytes, dict]:
    body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    return body, {"Content-Type": "application/x-www-form-urlencoded"}


def _intent_summary(pi: dict) -> dict:
    return {
        "id": pi.get("id"),
        "amount": pi.get("amount"),
        "currency": pi.get("currency"),
        "status": pi.get("status"),
        "description": pi.get("description"),
        "client_secret": pi.get("client_secret"),
        "created": pi.get("created"),
    }


def get_tools(config: dict | None = None) -> list[Tool]:
    def payments_create_intent(amount: int, currency: str = "usd",
                               description: str = "") -> dict | str:
        """Create a Stripe payment intent. HIGH RISK: gated, no charge
        happens until the intent is confirmed with user approval."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            amount_int = int(amount)
        except (TypeError, ValueError):
            return "ERROR: amount must be an integer in minor units (cents)"
        if amount_int <= 0:
            return "ERROR: amount must be a positive integer in minor units"
        currency = (currency or "").strip().lower()
        if not re.fullmatch(r"[a-z]{3}", currency):
            return "ERROR: currency must be a 3-letter ISO code (e.g. usd)"
        form: dict[str, Any] = {"amount": str(amount_int),
                                "currency": currency}
        if description and description.strip():
            form["description"] = description.strip()[:500]
        body, headers = _form(form)
        try:
            data = client.request("POST", "/v1/payment_intents",
                                  raw_body=body, headers=headers)
        except HttpError as exc:
            return f"ERROR: create payment intent failed: {exc}"
        return _intent_summary(data or {})

    def payments_get_intent(payment_intent_id: str) -> dict | str:
        """Inspect a Stripe payment intent by id. HIGH RISK: gated."""
        client = _client(config)
        if client is None:
            return _no_auth()
        pid = (payment_intent_id or "").strip()
        if not _PI_ID.match(pid):
            return "ERROR: payment_intent_id must look like pi_…"
        try:
            data = client.request("GET", f"/v1/payment_intents/{pid}")
        except HttpError as exc:
            return f"ERROR: get payment intent failed: {exc}"
        return _intent_summary(data or {})

    return [
        Tool(
            name="payments_create_intent",
            description="Create a Stripe payment intent (prepares a "
                        "charge; does not charge by itself). HIGH RISK: "
                        "gated — needs user approval every time. amount is "
                        "in minor units (cents).",
            json_schema={"type": "object",
                         "properties": {
                             "amount": {"type": "integer",
                                        "description": "minor units, e.g. "
                                                       "1000 = $10.00"},
                             "currency": {"type": "string"},
                             "description": {"type": "string"}},
                         "required": ["amount"]},
            risk="high", func=payments_create_intent),
        Tool(
            name="payments_get_intent",
            description="Inspect a Stripe payment intent's status by id. "
                        "HIGH RISK: gated.",
            json_schema={"type": "object",
                         "properties": {
                             "payment_intent_id": {"type": "string"}},
                         "required": ["payment_intent_id"]},
            risk="high", func=payments_get_intent),
    ]


__all__ = ["get_tools", "ENV_VAR", "TOKEN_FILE"]
