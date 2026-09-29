---
name: shopping
description: Product search with names, images and links. Bundled real provider covers groceries via OpenFoodFacts; general shopping needs a user-plugged provider.
version: 0.1.0
when: shopping, buy, price, product, deal, compare prices, groceries, where to buy
---

# Shopping

Product search with an honest, pluggable provider model.

Tool:

- `shopping_search(query, limit, provider)` — search products. Default
  provider `openfoodfacts` (real, keyless) covers **grocery/food
  products**: names, brands, images, nutrition grade, product pages.
  Price data is rarely present in OpenFoodFacts and comes back empty
  most of the time — that's the data, not a bug.

## Setup

- Groceries: nothing. `provider="openfoodfacts"` works out of the box
  (`https://world.openfoodfacts.org`, community database).
- General products (electronics, clothing, …): there is **no bundled
  provider** — public product feeds with reliable prices require a
  commercial API key (e.g. a shopping/affiliate API you subscribe to).
  Plug one in (see below) instead of expecting magic.

## Plugging in a provider

Register a provider in code before loading the skill:

```python
from agentkai.skills_bundle.shopping import tools as shopping_tools

def my_search(query: str, limit: int) -> list[dict]:
    # call your shopping API here; return a list of dicts with keys:
    # name (str), price (number|None), currency (str|None),
    # image_url (str|None), product_url (str|None), brand (str|None)
    ...

shopping_tools.register_provider("my_shop", my_search)
```

or pass providers through the skill config:

```python
loader = SkillLoader(config={"shopping_providers": {"my_shop": my_search}})
```

Then call `shopping_search(query, provider="my_shop")`. The tool
validates the returned shape loosely and passes items through.

## Usage notes

- Never present a product as purchasable through the agent unless the
  provider actually supports checkout — most providers are search-only.
- Prices move; always show the provider name next to a price.

## What's real / what's not

- REAL: grocery/food product search (OpenFoodFacts); any provider you
  plug in yourself.
- NOT PROVIDED: universal live prices, one-click checkout, order
  tracking. The agent does not fake a store.
