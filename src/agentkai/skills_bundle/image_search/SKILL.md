---
name: image_search
description: Search the web for images by keyword; returns direct image URLs plus source pages and license info (Openverse).
version: 0.1.0
when: image, images, picture, photo, illustration, wallpaper, find an image of, show me
---

# Image Search

Keyword image search powered by **Openverse** (api.openverse.org), an
aggregator of openly-licensed images. No API key needed for basic use.

Tool:

- `image_search(query, limit, license_type)` — returns direct `image_url`
  values plus `source_page`, `license`, `creator`, and dimensions.

## Setup

None for basic use. Anonymous Openverse access is rate-limited; if you
hit limits, register a free application at https://api.openverse.org/
for higher quotas.

## Usage notes

- Prefer `source_page` over hotlinking `image_url` when publishing:
  link the creator's page and keep the attribution Openverse returns.
- `license_type`: `all` (default), `commercial` (commercial use allowed),
  or `modification` (adaptations allowed). This filters by the license
  tag — it is not legal advice; verify the license on the source page
  before commercial use.
- Results are ranked by Openverse relevance, not by freshness.

## What's real / what's not

- REAL: keyword image search over openly-licensed images with direct
  URLs, source pages, licenses, and creators.
- NOT PROVIDED: reverse image search, face/person identification
  (never use this to identify people), image generation or editing —
  see the `media` skill for generation.
