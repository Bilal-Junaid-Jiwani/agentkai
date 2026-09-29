# Media generation

Image tools for the agent: `generate_image`, `edit_image`, and an
experimental `generate_video`. Read the limits first — they're the point.

## Honest limits

- **Image generation is never free-unlimited.** Two real providers:
  1. **OpenAI Images API** (`gpt-image-1`) — a **paid** API. Every call
     spends your money on your key. The tools are medium risk, so the
     permission gate asks before each call.
  2. **Local Stable Diffusion** — the AUTOMATIC1111 web UI API, if you
     run it yourself. Free after your own hardware.
- **No provider, no image.** Without a key or a reachable local server,
  the tools return a configuration error. Nothing is faked.
- **Video is experimental.** AgentKai ships **no** bundled video provider;
  `generate_video` returns an explanatory error until you register one
  (see `VideoProvider` in `agentkai/media.py`).

## Setup

**Option A — OpenAI (paid):**

1. Get a key at https://platform.openai.com/api-keys (billing enabled —
   image calls are charged per image).
2. `export OPENAI_API_KEY=...`, or create `~/.agentkai/openai_token.json`:
   ```json
   { "access_token": "<your-key>" }
   ```

**Option B — local Stable Diffusion (free after hardware):**

1. Install the AUTOMATIC1111 Stable Diffusion web UI, launch with `--api`
   (default `http://127.0.0.1:7860`).
2. No key needed. Set `AGENTKAI_IMAGE_PROVIDER=local_sd` to force it.

Provider selection order: `config["media"]["image_provider"]` →
`AGENTKAI_IMAGE_PROVIDER` env → auto (OpenAI key present → OpenAI; local
SD answering → local SD; else an honest error).

## Editing

`edit_image` does inpainting/edits (OpenAI provider; mask optional).
Generated files land in `~/.agentkai/media/` with unique names.

## What's not here

- No bundled video generation. The `generate_video` entry point exists
  so a future provider can plug in; today it tells you exactly that.
- No face/person identification — that's a hard no, not a missing feature.
