---
name: media
description: Generate images from text prompts (OpenAI Images API or a local Stable Diffusion server), edit/inpaint images, and a stub-backed experimental video hook.
version: 0.1.0
when: image, picture, generate image, draw, photo, edit image, inpaint, video generation, stable diffusion
---

# Media generation

Image tools for the agent: `generate_image`, `edit_image`, and an
experimental `generate_video`.

## Honest limits — read first

- **Image generation is never free-unlimited.** Two real providers exist:
  1. **OpenAI Images API** (`gpt-image-1`) — a **paid** API. Every call
     spends the user's money on their key. The tool descriptions say this
     and the tools are medium risk, so the permission gate asks first.
  2. **Local Stable Diffusion** — the AUTOMATIC1111 web UI API, only if
     the user actually runs it.
- **No provider, no image.** Without a key or a reachable local server
  the tools return a configuration error. Nothing is faked.
- **Video is experimental.** agentkai ships *no* bundled video provider;
  `generate_video` returns an explanatory error until the user registers
  one (see `VideoProvider` in `agentkai/media.py`).

## Setup

### Option A — OpenAI (paid)

1. Get an API key at https://platform.openai.com/api-keys (billing
   enabled — image calls are charged per image).
2. Set `OPENAI_API_KEY`, or create `~/.agentkai/openai_token.json`:
   ```json
   { "access_token": "<your-key>" }
   ```

### Option B — local Stable Diffusion (free after your own hardware)

1. Install the AUTOMATIC1111 Stable Diffusion web UI and launch it with
   the `--api` flag (default `http://127.0.0.1:7860`).
2. No key needed. Set `AGENTKAI_IMAGE_PROVIDER=local_sd` to force it, or
   let auto-detection find the server.

Provider selection order: `config["media"]["image_provider"]` →
`AGENTKAI_IMAGE_PROVIDER` env → auto (OpenAI key present → OpenAI;
local SD answering → local SD; else an honest error).

## Output

Files land in `~/.agentkai/media/` with unique timestamped names
(`img-YYYYMMDD-HHMMSS-<rand>-<prompt-slug>.png`). Tools return the saved
file path; the agent can then show or attach it.

## Usage notes

- `generate_image` snaps arbitrary sizes to the nearest OpenAI-supported
  size (1024x1024, 1536x1024, 1024x1536); local SD uses the exact WxH.
- `edit_image` needs the OpenAI provider; `mask` is optional (white =
  repaint area) for inpainting.
- Warn the user about cost before generating batches with the OpenAI
  provider.
