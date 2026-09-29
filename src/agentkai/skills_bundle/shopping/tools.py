"""Shopping skill tools: pluggable providers + bundled OpenFoodFacts.

Honest design: only OpenFoodFacts (groceries) is bundled and real.
General product search needs a user-registered provider — see SKILL.md.
"""
from __future__ import annotations

from typing import Any, Callable

from agentkai.skills_bundle._common import test_transport
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

OFF_BASE = "https://world.openfoodfacts.org"

# Provider contract: search(query: str, limit: int) -> list[dict] where each
# dict may carry: name, price, currency, image_url, product_url, brand.
ProviderFn = Callable[[str, int], list]

_registry: dict[str, ProviderFn] = {}


def register_provider(name: str, search_fn: ProviderFn) -> None:
    """Register a custom shopping provider (code-level registration)."""
    if not name or not name.strip():
        raise ValueError("provider name is required")
    if not callable(search_fn):
        raise ValueError("search_fn must be callable")
    _registry[name.strip()] = search_fn


def _openfoodfacts_search(query: str, limit: int,
                          config: dict | None) -> list:
    client = HttpClient(OFF_BASE, transport=test_transport(config))
    data = client.request(
        "GET", "/cgi/search.pl",
        params={"search_terms": query, "search_simple": "1",
                "action": "process", "json": "1",
                "page_size": str(limit)})
    products = (data or {}).get("products") or []
    out = []
    for p in products:
        code = p.get("code")
        out.append({
            "name": p.get("product_name"),
            "brand": p.get("brands"),
            "price": None,  # OpenFoodFacts rarely carries prices
            "currency": None,
            "image_url": p.get("image_url"),
            "product_url": (f"https://world.openfoodfacts.org/product/{code}"
                            if code else None),
            "nutrition_grade": (p.get("nutrition_grades") or
                                p.get("nutriscore_grade")),
        })
    return out


def _providers(config: dict | None) -> dict[str, ProviderFn]:
    providers: dict[str, ProviderFn] = dict(_registry)
    cfg = (config or {}).get("shopping_providers") or {}
    for name, fn in cfg.items():
        if callable(fn):
            providers[name] = fn
    providers.setdefault(
        "openfoodfacts",
        lambda q, n: _openfoodfacts_search(q, n, config))
    return providers


def _normalize(items: Any, provider: str) -> list | str:
    if not isinstance(items, list):
        return (f"ERROR: provider {provider!r} returned "
                f"{type(items).__name__}, expected a list")
    out = []
    for it in items:
        if not isinstance(it, dict):
            return (f"ERROR: provider {provider!r} returned a non-dict "
                    f"item")
        out.append({
            "name": it.get("name"),
            "brand": it.get("brand"),
            "price": it.get("price"),
            "currency": it.get("currency"),
            "image_url": it.get("image_url"),
            "product_url": it.get("product_url"),
        })
    return out


def get_tools(config: dict | None = None) -> list[Tool]:
    def shopping_search(query: str, limit: int = 10,
                        provider: str = "openfoodfacts") -> list | str:
        """Search products via a provider. Bundled: openfoodfacts
        (groceries, real, keyless). Others must be registered."""
        if not query or not query.strip():
            return "ERROR: query is required"
        limit = min(max(int(limit), 1), 25)
        providers = _providers(config)
        if provider not in providers:
            available = ", ".join(sorted(providers)) or "none"
            return (
                f"ERROR: unknown shopping provider {provider!r}. "
                f"Available: {available}. The bundled 'openfoodfacts' "
                f"provider covers groceries only; for general products "
                f"register your own provider — see the shopping SKILL.md.")
        try:
            items = providers[provider](query.strip(), limit)
        except HttpError as exc:
            return f"ERROR: shopping search failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - provider bugs surface cleanly
            return f"ERROR: provider {provider!r} raised: {exc}"
        return _normalize(items, provider)

    return [
        Tool(
            name="shopping_search",
            description="Search products by keyword via a provider. "
                        "Bundled real provider 'openfoodfacts' covers "
                        "groceries (keyless). General shopping needs a "
                        "user-registered provider; see SKILL.md.",
            json_schema={"type": "object",
                         "properties": {
                             "query": {"type": "string"},
                             "limit": {"type": "integer"},
                             "provider": {"type": "string"}},
                         "required": ["query"]},
            risk="low", func=shopping_search),
    ]


__all__ = ["get_tools", "register_provider"]
