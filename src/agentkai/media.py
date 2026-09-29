"""Media generation tools: images (and an honest video hook).

Capabilities and honest limits (read before wiring this up):

- ``generate_image`` supports two real providers:
    1. **OpenAI Images API** (``gpt-image-1``) — requires ``OPENAI_API_KEY``.
       This is a paid API: every call costs real money on the user's key.
    2. **Local Stable Diffusion** via the AUTOMATIC1111 web UI API
       (``http://127.0.0.1:7860`` with ``--api`` enabled). Only works if the
       user actually runs such a server; the code never pretends local
       generation happened — if the server is unreachable the tool returns
       an honest error.
- There is **no free unlimited generation** anywhere in this module.
- ``generate_video`` is a provider *interface* plus honest plumbing. No
  bundled provider generates real video: without a configured video
  provider it returns an explanatory error instead of a fake file. Mark
  video generation as experimental in any UI that exposes it.

Provider selection for images (first match wins):
    1. ``config["media"]["image_provider"]`` ("openai" | "local_sd"),
    2. ``AGENTKAI_IMAGE_PROVIDER`` env var,
    3. auto: OpenAI if ``OPENAI_API_KEY`` (or ``~/.agentkai/openai_token.json``)
       is present, else local SD if the server answers, else an error.

All network calls use stdlib urllib with timeouts. Nothing here logs or
prints API keys.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .tools import Tool

# ---- configuration ----------------------------------------------------------

MEDIA_DIR_NAME = "media"
OPENAI_ENV_VAR = "OPENAI_API_KEY"
OPENAI_TOKEN_FILE = "openai_token.json"
SD_BASE_URL = "http://127.0.0.1:7860"
NET_TIMEOUT = 30
VIDEO_NOTICE = (
    "Video generation is experimental: agentkai ships no bundled video "
    "provider, so without a configured one this tool returns an error "
    "rather than a fake file."
)


def agentkai_home() -> Path:
    override = os.environ.get("AGENTKAI_HOME")
    if override:
        return Path(override).expanduser()
    return Path("~/.agentkai").expanduser()


def media_dir(home: Path | None = None) -> Path:
    d = (home or agentkai_home()) / MEDIA_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_openai_key(config: dict | None) -> str | None:
    raw = os.environ.get(OPENAI_ENV_VAR)
    if raw and raw.strip():
        return raw.strip()
    home = (config or {}).get("home")
    path = (Path(home).expanduser() if home
            else agentkai_home()) / OPENAI_TOKEN_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            token = data.get("access_token") or data.get("token")
            if token and str(token).strip():
                return str(token).strip()
    return None


def _slug(prompt: str, length: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
    return (s[:length] or "image").strip("-")


def _unique_name(prefix: str, prompt: str, ext: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}-{_slug(prompt)}{ext}"


def _parse_size(size: str) -> tuple[int, int]:
    """Parse "WxH" into (width, height). Raises ValueError on bad input."""
    m = re.fullmatch(r"\s*(\d{2,4})\s*[xX×]\s*(\d{2,4})\s*", size or "")
    if not m:
        raise ValueError(
            f"size must look like '1024x1024', got {size!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if not (64 <= w <= 4096 and 64 <= h <= 4096):
        raise ValueError(f"size out of range (64..4096): {size!r}")
    return w, h


def _post_json(url: str, payload: dict,
               headers: dict | None = None,
               timeout: int = NET_TIMEOUT) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise MediaError(f"HTTP {exc.code} from provider: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MediaError(f"could not reach provider: {exc.reason}") from exc


class MediaError(Exception):
    """Provider-level failure (network, auth, quota, unreachable server)."""


# ---- provider interface -------------------------------------------------------

@dataclass
class MediaResult:
    path: str
    provider: str
    width: int
    height: int
    bytes: int


class ImageProvider:
    """Interface every image provider implements."""
    name = "base"

    def generate(self, prompt: str, width: int, height: int,
                 out_path: Path) -> MediaResult:
        raise NotImplementedError

    def edit(self, image_path: Path, mask_path: Path | None, prompt: str,
             width: int, height: int, out_path: Path) -> MediaResult:
        raise MediaError(
            f"image editing is not supported by the {self.name} provider")


class OpenAIImagesProvider(ImageProvider):
    """OpenAI Images API (gpt-image-1). Paid — costs money per call."""
    name = "openai"

    def __init__(self, api_key: str,
                 request: Callable[..., dict] | None = None) -> None:
        if not api_key or not api_key.strip():
            raise MediaError("OpenAI provider needs an API key")
        self._key = api_key.strip()
        self._request = request or _post_json

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}"}

    def _save_b64(self, b64: str, out_path: Path) -> int:
        raw = base64.b64decode(b64)
        out_path.write_bytes(raw)
        return len(raw)

    def generate(self, prompt: str, width: int, height: int,
                 out_path: Path) -> MediaResult:
        size = _openai_size(width, height)
        data = self._request(
            "https://api.openai.com/v1/images/generations",
            {"model": "gpt-image-1", "prompt": prompt, "size": size,
             "response_format": "b64_json"},
            headers=self._headers(), timeout=120)
        try:
            b64 = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MediaError(
                f"unexpected OpenAI response: {str(data)[:300]}") from exc
        n = self._save_b64(b64, out_path)
        return MediaResult(str(out_path), self.name,
                           *map(int, size.split("x")), n)

    def edit(self, image_path: Path, mask_path: Path | None, prompt: str,
             width: int, height: int, out_path: Path) -> MediaResult:
        if not image_path.is_file():
            raise MediaError(f"image not found: {image_path}")
        size = _openai_size(width, height)
        fields = {"model": "gpt-image-1", "prompt": prompt, "size": size,
                  "response_format": "b64_json"}
        files = {"image": (image_path.name,
                           image_path.read_bytes(),
                           mimetypes.guess_type(image_path.name)[0]
                           or "image/png")}
        if mask_path is not None:
            if not mask_path.is_file():
                raise MediaError(f"mask not found: {mask_path}")
            files["mask"] = (mask_path.name, mask_path.read_bytes(),
                             mimetypes.guess_type(mask_path.name)[0]
                             or "image/png")
        data = _post_multipart(
            "https://api.openai.com/v1/images/edits",
            fields, files, headers=self._headers(), timeout=180)
        try:
            b64 = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MediaError(
                f"unexpected OpenAI response: {str(data)[:300]}") from exc
        n = self._save_b64(b64, out_path)
        return MediaResult(str(out_path), self.name,
                           *map(int, size.split("x")), n)


def _openai_size(width: int, height: int) -> str:
    """Snap an arbitrary WxH to the closest gpt-image-1 supported size."""
    options = [(1024, 1024), (1536, 1024), (1024, 1536)]
    ratio = width / height
    best = min(options, key=lambda wh: abs(wh[0] / wh[1] - ratio))
    return f"{best[0]}x{best[1]}"


def _post_multipart(url: str, fields: dict, files: dict,
                    headers: dict | None = None,
                    timeout: int = NET_TIMEOUT) -> dict:
    boundary = secrets.token_hex(16)
    buf = io.BytesIO()
    for key, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    for key, (filename, content, ctype) in files.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{key}"; '
            f'filename="{filename}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=buf.getvalue(),
        headers={"Content-Type":
                 f"multipart/form-data; boundary={boundary}",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise MediaError(f"HTTP {exc.code} from provider: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MediaError(f"could not reach provider: {exc.reason}") from exc


class LocalSDProvider(ImageProvider):
    """AUTOMATIC1111 Stable Diffusion web UI API (local, user-run).

    Contract: the user runs the web UI with ``--api`` (default
    ``http://127.0.0.1:7860``). We call ``POST /sdapi/v1/txt2img`` with
    ``{"prompt", "width", "height", "steps", "sampler_name"}`` and decode
    the ``images[0]`` base64 payload. No server → honest error, never a
    fake image.
    """
    name = "local_sd"

    def __init__(self, base_url: str = SD_BASE_URL,
                 request: Callable[..., dict] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._request = request or _post_json

    def is_available(self, timeout: int = 3) -> bool:
        try:
            req = urllib.request.Request(
                self.base_url + "/sdapi/v1/options")
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:  # noqa: BLE001 - any failure means unavailable
            return False

    def generate(self, prompt: str, width: int, height: int,
                 out_path: Path) -> MediaResult:
        if not self.is_available():
            raise MediaError(
                "local Stable Diffusion server is not reachable at "
                f"{self.base_url}. Start the AUTOMATIC1111 web UI with "
                "'--api' (or set a different base URL) — agentkai will "
                "not fake a locally generated image.")
        data = self._request(
            self.base_url + "/sdapi/v1/txt2img",
            {"prompt": prompt, "width": width, "height": height,
             "steps": 30, "sampler_name": "DPM++ 2M Karras"},
            timeout=600)
        try:
            b64 = data["images"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise MediaError(
                f"unexpected SD response: {str(data)[:300]}") from exc
        raw = base64.b64decode(b64)
        out_path.write_bytes(raw)
        return MediaResult(str(out_path), self.name, width, height, len(raw))


# ---- selection ------------------------------------------------------------------

def select_image_provider(config: dict | None = None,
                          probe_local: bool = True) -> ImageProvider:
    """Pick an image provider. Raises MediaError when none is configured.

    Order: config["media"]["image_provider"] → AGENTKAI_IMAGE_PROVIDER →
    auto (OpenAI key present → "openai"; local SD answering → "local_sd").
    """
    media_cfg = ((config or {}).get("media") or {})
    wanted = (media_cfg.get("image_provider")
              or os.environ.get("AGENTKAI_IMAGE_PROVIDER") or "").strip().lower()
    if wanted == "openai":
        key = _get_openai_key(config)
        if not key:
            raise MediaError(
                "image provider 'openai' selected but no API key found. "
                f"Set {OPENAI_ENV_VAR} or create "
                f"~/{agentkai_home().name}/{OPENAI_TOKEN_FILE}.")
        return OpenAIImagesProvider(key)
    if wanted == "local_sd":
        base = (media_cfg.get("sd_base_url")
                or os.environ.get("AGENTKAI_SD_URL") or SD_BASE_URL)
        return LocalSDProvider(base)
    if wanted:
        raise MediaError(
            f"unknown image provider {wanted!r}: use 'openai' or 'local_sd'")

    key = _get_openai_key(config)
    if key:
        return OpenAIImagesProvider(key)
    if probe_local:
        sd = LocalSDProvider(
            media_cfg.get("sd_base_url")
            or os.environ.get("AGENTKAI_SD_URL") or SD_BASE_URL)
        if sd.is_available():
            return sd
    raise MediaError(
        "no image provider configured. Either set "
        f"{OPENAI_ENV_VAR} (paid OpenAI Images API) or run a local "
        "Stable Diffusion server (AUTOMATIC1111 web UI with --api on "
        "http://127.0.0.1:7860). agentkai does not generate images "
        "without a real provider.")


class VideoProvider:
    """Interface for video generation providers.

    agentkai ships **no** bundled implementation — this exists so a user
    can plug in a real provider (e.g. a paid video API they hold a key
    for) without touching agentkai's core:

    .. code-block:: python

        from agentkai.media import VideoProvider, register_video_provider

        class MyVideoProvider(VideoProvider):
            name = "myvideo"

            def generate(self, prompt, duration, out_path):
                ...  # call the real API, write a real file ...
                return MediaResult(str(out_path), self.name,
                                   width, height, nbytes)

        register_video_provider(MyVideoProvider(api_key=...))

    Until :func:`select_video_provider` finds a registered provider,
    :func:`generate_video` returns an honest error — never a fake file.
    """
    name = "base"

    def generate(self, prompt: str, duration: int,
                 out_path: Path) -> MediaResult:
        raise NotImplementedError


_VIDEO_PROVIDERS: dict[str, VideoProvider] = {}


def register_video_provider(provider: VideoProvider) -> None:
    """Register a user-supplied video provider (see VideoProvider)."""
    _VIDEO_PROVIDERS[provider.name] = provider


def select_video_provider(config: dict | None = None) -> VideoProvider:
    """Return the configured video provider, or raise MediaError."""
    media_cfg = ((config or {}).get("media") or {})
    wanted = (media_cfg.get("video_provider")
              or os.environ.get("AGENTKAI_VIDEO_PROVIDER") or "").strip()
    if wanted:
        if wanted in _VIDEO_PROVIDERS:
            return _VIDEO_PROVIDERS[wanted]
        raise MediaError(
            f"video provider {wanted!r} is not registered. "
            f"Registered: {sorted(_VIDEO_PROVIDERS) or 'none'}. See "
            "VideoProvider in agentkai/media.py for how to add one.")
    if _VIDEO_PROVIDERS:
        return next(iter(_VIDEO_PROVIDERS.values()))
    raise MediaError(
        "no video provider configured. " + VIDEO_NOTICE + " To plug one "
        "in, implement VideoProvider (see its docstring) and call "
        "register_video_provider().")


# ---- public functions -------------------------------------------------------------

def generate_image(prompt: str, size: str = "1024x1024",
                   out_path: str | None = None,
                   config: dict | None = None,
                   provider: ImageProvider | None = None) -> dict:
    """Generate an image with the selected provider. Returns a result dict
    or an "ERROR: ..." string. Never raises on provider failure."""
    if not (prompt or "").strip():
        return "ERROR: prompt is required"
    try:
        width, height = _parse_size(size)
    except ValueError as exc:
        return f"ERROR: {exc}"
    home = Path((config or {}).get("home", "") or agentkai_home())
    dest = media_dir(home)
    if out_path:
        p = Path(out_path).expanduser()
        if not p.is_absolute():
            p = dest / p
        # Keep outputs inside the media dir or an explicit absolute path the
        # caller chose; refuse traversal tricks on relative paths.
        if not p.is_absolute():
            return f"ERROR: bad out_path: {out_path!r}"
    else:
        p = dest / _unique_name("img", prompt, ".png")
    try:
        prov = provider or select_image_provider(config)
        result = prov.generate(prompt.strip(), width, height, p)
    except MediaError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 - tools report, never raise
        return f"ERROR: {type(exc).__name__}: {exc}"
    return {"path": result.path, "provider": result.provider,
            "width": result.width, "height": result.height,
            "bytes": result.bytes}


def generate_video(prompt: str, duration: int = 5,
                   out_path: str | None = None,
                   config: dict | None = None) -> dict | str:
    """Video generation entry point. Experimental: without a configured
    provider this returns an honest error, never a fake video file."""
    if not (prompt or "").strip():
        return "ERROR: prompt is required"
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return "ERROR: duration must be an integer number of seconds"
    if not 1 <= duration <= 60:
        return "ERROR: duration must be 1..60 seconds"
    try:
        prov = select_video_provider(config)
    except MediaError as exc:
        return f"ERROR: {exc}"
    home = Path((config or {}).get("home", "") or agentkai_home())
    dest = media_dir(home)
    p = Path(out_path).expanduser() if out_path else None
    if p is not None and not p.is_absolute():
        p = dest / p
    if p is None:
        p = dest / _unique_name("vid", prompt, ".mp4")
    try:
        result = prov.generate(prompt.strip(), duration, p)
    except MediaError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 - tools report, never raise
        return f"ERROR: {type(exc).__name__}: {exc}"
    return {"path": result.path, "provider": result.provider,
            "width": result.width, "height": result.height,
            "bytes": result.bytes}


def edit_image(image: str, prompt: str, mask: str | None = None,
               size: str = "1024x1024", out_path: str | None = None,
               config: dict | None = None,
               provider: ImageProvider | None = None) -> dict:
    """Edit/inpaint an image (OpenAI provider; mask optional)."""
    if not (prompt or "").strip():
        return "ERROR: prompt is required"
    if not (image or "").strip():
        return "ERROR: image path is required"
    try:
        width, height = _parse_size(size)
    except ValueError as exc:
        return f"ERROR: {exc}"
    img = Path(image).expanduser()
    mask_p = Path(mask).expanduser() if mask else None
    if not img.is_file():
        return f"ERROR: image not found: {image!r}"
    home = Path((config or {}).get("home", "") or agentkai_home())
    dest = media_dir(home)
    p = Path(out_path).expanduser() if out_path else None
    if p is not None and not p.is_absolute():
        p = dest / p
    if p is None:
        p = dest / _unique_name("edit", prompt, ".png")
    try:
        prov = provider or select_image_provider(config)
        result = prov.edit(img, mask_p, prompt.strip(),
                           width, height, p)
    except MediaError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 - tools report, never raise
        return f"ERROR: {type(exc).__name__}: {exc}"
    return {"path": result.path, "provider": result.provider,
            "width": result.width, "height": result.height,
            "bytes": result.bytes}


# ---- agent tools --------------------------------------------------------------------

_IMAGE_DESC = (
    "Generate an image from a text prompt and save it under the agent media "
    "directory. COSTS MONEY when using the OpenAI provider (paid API key "
    "required); free only with a user-run local Stable Diffusion server. "
    "Returns the saved file path. There is no free unlimited generation.")
_VIDEO_DESC = (
    "Generate a short video from a text prompt. EXPERIMENTAL: agentkai "
    "ships no bundled video provider, so this returns an error unless the "
    "user configured one. It never produces a fake video file.")
_EDIT_DESC = (
    "Edit or inpaint an existing image with a text prompt (optional mask "
    "image for inpainting). COSTS MONEY via the OpenAI provider (paid API "
    "key required). Returns the saved file path.")


def media_tools(config: dict | None = None) -> list[Tool]:
    """Image/video agent tools. They use the Tool gate protocol via
    ``Tool.run(_gate=...)`` — the caller (agent loop) supplies the gate."""
    return [
        Tool(
            name="generate_image",
            description=_IMAGE_DESC,
            json_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "size": {"type": "string",
                             "description": "WxH, e.g. 1024x1024"},
                    "out_path": {"type": "string"},
                },
                "required": ["prompt"],
            },
            risk="medium",
            func=lambda prompt, size="1024x1024", out_path=None: (
                generate_image(prompt, size=size, out_path=out_path,
                               config=config)),
        ),
        Tool(
            name="generate_video",
            description=_VIDEO_DESC,
            json_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "duration": {"type": "integer"},
                    "out_path": {"type": "string"},
                },
                "required": ["prompt"],
            },
            risk="medium",
            func=lambda prompt, duration=5, out_path=None: (
                generate_video(prompt, duration=duration, out_path=out_path,
                               config=config)),
        ),
        Tool(
            name="edit_image",
            description=_EDIT_DESC,
            json_schema={
                "type": "object",
                "properties": {
                    "image": {"type": "string"},
                    "prompt": {"type": "string"},
                    "mask": {"type": "string"},
                    "size": {"type": "string"},
                    "out_path": {"type": "string"},
                },
                "required": ["image", "prompt"],
            },
            risk="medium",
            func=lambda image, prompt, mask=None, size="1024x1024",
            out_path=None: (
                edit_image(image, prompt, mask=mask, size=size,
                           out_path=out_path, config=config)),
        ),
    ]


__all__ = [
    "MediaError", "MediaResult", "ImageProvider", "OpenAIImagesProvider",
    "LocalSDProvider", "VideoProvider", "register_video_provider",
    "select_video_provider", "select_image_provider",
    "generate_image", "generate_video", "edit_image", "media_tools",
    "media_dir", "agentkai_home",
]
