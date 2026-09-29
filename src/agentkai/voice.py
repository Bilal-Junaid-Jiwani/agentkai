"""Voice capabilities: speech-to-text (STT) and text-to-speech (TTS).

Two honest provider tiers, never faked:

STT (``transcribe``):
  1. ``local`` — faster-whisper on this machine (optional dependency,
     ``pip install agentkai[voice]``). Private: audio never leaves the box.
  2. ``openai`` — OpenAI Whisper API (needs ``OPENAI_API_KEY``).

TTS (``speak``):
  1. ``openai`` — OpenAI TTS API (needs ``OPENAI_API_KEY``). Natural voices.
  2. ``local`` — ``espeak``/``espeak-ng`` (Linux) or ``say`` (macOS) when
     installed. Robotic, but free and fully offline.

Deliberately NOT included: free "Edge-TTS"-style endpoints. They are
undocumented, break without notice, and depend on scraping a vendor's
website — shipping that as a feature would be dishonest. See
``provider_matrix()`` for what is actually available on this machine.

Audio formats: mp3, wav, m4a, ogg/oga, opus, flac, webm, aac. Non-wav input
is converted to 16 kHz mono WAV with ffmpeg when ffmpeg is installed;
otherwise a clear error explains what is missing.

Messaging hook: channels download voice notes themselves (Telegram,
WhatsApp bridge, …). Call :func:`preprocess_voice_note` on the downloaded
file *before* building the channel's ``InboundMessage`` and put the
returned text in ``msg.text``. This module never touches channel code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

from .tools import Tool

__all__ = [
    "VoiceError",
    "STTProvider",
    "TTSProvider",
    "LocalWhisperSTT",
    "OpenAIWhisperSTT",
    "LocalEspeakTTS",
    "OpenAITTS",
    "select_stt",
    "select_tts",
    "transcribe",
    "speak",
    "preprocess_voice_note",
    "provider_matrix",
    "voice_tools",
    "SUPPORTED_EXTENSIONS",
]

OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_TOKEN_FILE = "openai_token.json"

SUPPORTED_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".ogg", ".oga",
    ".opus", ".flac", ".webm", ".aac",
}

OPENAI_TTS_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
MAX_TTS_CHARS = 4000


class VoiceError(Exception):
    """Configuration / availability problem (missing dep, key, binary…)."""


# ---- availability probes (small seams so tests can monkeypatch) -------------

def faster_whisper_available() -> bool:
    return importlib_util.find_spec("faster_whisper") is not None


def openai_package_available() -> bool:
    return importlib_util.find_spec("openai") is not None


def openai_key() -> str | None:
    """OPENAI_API_KEY from env, else ~/.agentkai/openai_token.json."""
    raw = os.environ.get(OPENAI_API_KEY_ENV)
    if raw and raw.strip():
        return raw.strip()
    path = Path("~/.agentkai").expanduser() / OPENAI_TOKEN_FILE
    if path.exists():
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            token = (data.get("access_token") or data.get("token")
                     if isinstance(data, dict) else None)
            if token and str(token).strip():
                return str(token).strip()
        except (OSError, ValueError):
            return None
    return None


def openai_available() -> bool:
    return openai_package_available() and openai_key() is not None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def local_tts_command() -> str | None:
    """First usable local TTS binary, or None."""
    for cmd in ("espeak-ng", "espeak", "say"):
        if shutil.which(cmd):
            return cmd
    return None


def provider_matrix() -> dict[str, dict[str, Any]]:
    """Honest capability report for this machine. No faking."""
    cmd = local_tts_command()
    return {
        "stt.local (faster-whisper)": {
            "available": faster_whisper_available(),
            "note": "offline, private; pip install agentkai[voice]",
        },
        "stt.openai (whisper-1)": {
            "available": openai_available(),
            "note": "needs OPENAI_API_KEY; audio leaves the machine",
        },
        "tts.openai (tts-1)": {
            "available": openai_available(),
            "note": "needs OPENAI_API_KEY; voices: "
                    + ", ".join(OPENAI_TTS_VOICES),
        },
        "tts.local": {
            "available": cmd is not None,
            "note": f"binary: {cmd}" if cmd else
                    "needs espeak/espeak-ng (Linux) or say (macOS)",
        },
    }


# ---- audio normalization -----------------------------------------------------

def _ensure_wav(audio_path: str | Path) -> Path:
    """Validate format; convert non-wav to 16 kHz mono wav via ffmpeg.

    Returns a Path to use for transcription. Converted files live in a
    temp dir and are the caller's responsibility to clean up (transcribe()
    handles it).
    """
    p = Path(audio_path).expanduser()
    if not p.exists():
        raise VoiceError(f"audio file not found: {audio_path}")
    if not p.is_file():
        raise VoiceError(f"not an audio file: {audio_path}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise VoiceError(
            f"unsupported audio format {ext or '(no extension)'} for "
            f"{p.name}; supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    if ext == ".wav":
        return p
    if not ffmpeg_available():
        raise VoiceError(
            f"{p.name} is {ext}, not wav, and ffmpeg is not installed — "
            f"install ffmpeg to convert it, or provide a .wav file")
    out = Path(tempfile.mkdtemp(prefix="agentkai-voice-")) / "audio.wav"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(p),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out.exists():
        raise VoiceError(
            f"ffmpeg could not convert {p.name}: "
            f"{proc.stderr.strip()[:200]}")
    return out


def _cleanup_converted(wav: Path, original: Path) -> None:
    if wav != original and "agentkai-voice-" in str(wav.parent):
        try:
            shutil.rmtree(wav.parent, ignore_errors=True)
        except OSError:
            pass


# ---- provider interfaces -----------------------------------------------------

class STTProvider(ABC):
    """Speech-to-text provider. ``transcribe`` returns plain text."""

    name: str = "stt"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """True when this provider can actually run here."""

    @abstractmethod
    def transcribe(self, audio_path: str | Path) -> str:
        ...


class TTSProvider(ABC):
    """Text-to-speech provider. ``synthesize`` writes an audio file."""

    name: str = "tts"
    default_extension: str = "wav"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        ...

    def voices(self) -> list[str]:
        return ["default"]

    @abstractmethod
    def synthesize(self, text: str, voice: str | None,
                   out_path: str | Path) -> Path:
        ...


# ---- STT providers ------------------------------------------------------------

class LocalWhisperSTT(STTProvider):
    """faster-whisper on this machine. Offline and private."""

    name = "local-whisper"

    def __init__(self, model: str = "base", _model: Any = None) -> None:
        self.model_name = model
        self._model = _model  # test seam: inject a fake model

    @classmethod
    def available(cls) -> bool:
        return faster_whisper_available()

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_name, device="auto", compute_type="int8")
        return self._model

    def transcribe(self, audio_path: str | Path) -> str:
        original = Path(audio_path).expanduser()
        wav = _ensure_wav(original)
        try:
            segments, _info = self._load().transcribe(
                str(wav), beam_size=5)
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            _cleanup_converted(wav, original)


class OpenAIWhisperSTT(STTProvider):
    """OpenAI Whisper API. Needs OPENAI_API_KEY; audio leaves the machine."""

    name = "openai-whisper"

    def __init__(self, model: str = "whisper-1",
                 api_key: str | None = None) -> None:
        self.model_name = model
        self.api_key = api_key or openai_key()

    @classmethod
    def available(cls) -> bool:
        return openai_available()

    def transcribe(self, audio_path: str | Path) -> str:
        if not openai_package_available():
            raise VoiceError(
                "openai package is not installed (pip install openai)")
        if not self.api_key:
            raise VoiceError(
                f"set {OPENAI_API_KEY_ENV} to use OpenAI Whisper")
        from openai import OpenAI
        original = Path(audio_path).expanduser()
        wav = _ensure_wav(original)
        try:
            client = OpenAI(api_key=self.api_key)
            with open(wav, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=self.model_name, file=f)
            return (getattr(resp, "text", "") or "").strip()
        finally:
            _cleanup_converted(wav, original)


def select_stt(prefer: str = "auto",
               provider: STTProvider | None = None) -> STTProvider:
    """Pick an STT provider. ``prefer``: "auto" | "local" | "openai"."""
    if provider is not None:
        return provider
    if prefer not in ("auto", "local", "openai"):
        raise VoiceError(f'unknown STT preference {prefer!r} '
                         f'(want "auto", "local" or "openai")')
    candidates: list[type[STTProvider]] = []
    if prefer in ("auto", "local"):
        candidates.append(LocalWhisperSTT)
    if prefer in ("auto", "openai"):
        candidates.append(OpenAIWhisperSTT)
    for cls in candidates:
        if cls.available():
            return cls()
    raise VoiceError(
        "no speech-to-text provider is available: "
        "install faster-whisper (pip install agentkai[voice]) for offline "
        f"transcription, or set {OPENAI_API_KEY_ENV} for the OpenAI Whisper "
        "API. See provider_matrix() for details.")


def transcribe(audio_path: str | Path, prefer: str = "auto",
               provider: STTProvider | None = None) -> str:
    """Transcribe an audio file to text. Raises VoiceError on problems."""
    return select_stt(prefer, provider).transcribe(audio_path)


# ---- TTS providers ------------------------------------------------------------

class LocalEspeakTTS(TTSProvider):
    """espeak/espeak-ng (Linux) or say (macOS). Free, offline, robotic."""

    name = "local-tts"

    def __init__(self, command: str | None = None) -> None:
        self.command = command or local_tts_command() or ""
        self.default_extension = "aiff" if self.command == "say" else "wav"

    @classmethod
    def available(cls) -> bool:
        return local_tts_command() is not None

    def voices(self) -> list[str]:
        if not self.command:
            return ["default"]
        try:
            if self.command == "say":
                proc = subprocess.run(
                    ["say", "-v", "?"], capture_output=True, text=True,
                    timeout=10)
                names = [line.split()[0] for line in proc.stdout.splitlines()
                         if line.strip()]
                return names or ["default"]
            proc = subprocess.run(
                [self.command, "--voices"], capture_output=True, text=True,
                timeout=10)
            names = []
            for line in proc.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    names.append(parts[3])
            return names or ["default"]
        except (OSError, subprocess.SubprocessError):
            return ["default"]

    def synthesize(self, text: str, voice: str | None,
                   out_path: str | Path) -> Path:
        if not self.command:
            raise VoiceError(
                "no local TTS binary found — install espeak/espeak-ng "
                "(Linux) or use macOS 'say'")
        out = Path(out_path)
        if self.command == "say":
            args = ["say", "-o", str(out)]
            if voice:
                args += ["-v", voice]
            args.append(text)
        else:
            args = [self.command, "-w", str(out)]
            if voice:
                args += ["-v", voice]
            args.append(text)
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=120)
        except subprocess.TimeoutExpired:
            raise VoiceError("local TTS timed out after 120s")
        if proc.returncode != 0:
            raise VoiceError(
                f"{self.command} failed: {proc.stderr.strip()[:200]}")
        return out


class OpenAITTS(TTSProvider):
    """OpenAI TTS API. Natural voices; needs OPENAI_API_KEY."""

    name = "openai-tts"
    default_extension = "mp3"

    def __init__(self, model: str = "tts-1",
                 api_key: str | None = None) -> None:
        self.model_name = model
        self.api_key = api_key or openai_key()

    @classmethod
    def available(cls) -> bool:
        return openai_available()

    def voices(self) -> list[str]:
        return list(OPENAI_TTS_VOICES)

    def synthesize(self, text: str, voice: str | None,
                   out_path: str | Path) -> Path:
        if not openai_package_available():
            raise VoiceError(
                "openai package is not installed (pip install openai)")
        if not self.api_key:
            raise VoiceError(
                f"set {OPENAI_API_KEY_ENV} to use OpenAI TTS")
        voice = voice or "alloy"
        if voice not in OPENAI_TTS_VOICES:
            raise VoiceError(
                f"unknown OpenAI voice {voice!r}; choose from "
                + ", ".join(OPENAI_TTS_VOICES))
        from openai import OpenAI
        out = Path(out_path)
        client = OpenAI(api_key=self.api_key)
        resp = client.audio.speech.create(
            model=self.model_name, voice=voice, input=text)
        if hasattr(resp, "write_to_file"):
            resp.write_to_file(str(out))
        else:  # pragma: no cover - older client shapes
            out.write_bytes(resp.content)
        return out


def select_tts(prefer: str = "auto",
               provider: TTSProvider | None = None) -> TTSProvider:
    """Pick a TTS provider. ``prefer``: "auto" | "openai" | "local".

    Auto prefers OpenAI (natural voices) when a key is configured, else
    falls back to the local binary.
    """
    if provider is not None:
        return provider
    if prefer not in ("auto", "openai", "local"):
        raise VoiceError(f'unknown TTS preference {prefer!r} '
                         f'(want "auto", "openai" or "local")')
    candidates: list[type[TTSProvider]] = []
    if prefer in ("auto", "openai"):
        candidates.append(OpenAITTS)
    if prefer in ("auto", "local"):
        candidates.append(LocalEspeakTTS)
    for cls in candidates:
        if cls.available():
            return cls()
    raise VoiceError(
        "no text-to-speech provider is available: "
        f"set {OPENAI_API_KEY_ENV} for OpenAI TTS, or install "
        "espeak/espeak-ng (Linux) / use macOS 'say' for offline synthesis. "
        "See provider_matrix() for details.")


def _default_out_path(extension: str) -> Path:
    d = Path("~/.agentkai/voice").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return d / f"tts-{stamp}-{uuid.uuid4().hex[:8]}.{extension}"


def speak(text: str, voice: str | None = None,
          out_path: str | Path | None = None, prefer: str = "auto",
          provider: TTSProvider | None = None) -> Path:
    """Synthesize *text* to an audio file. Returns the file path."""
    if not text or not text.strip():
        raise VoiceError("nothing to speak: text is empty")
    if len(text) > MAX_TTS_CHARS:
        raise VoiceError(
            f"text is {len(text)} chars, over the {MAX_TTS_CHARS} char "
            f"limit — split it into smaller chunks")
    prov = select_tts(prefer, provider)
    out = Path(out_path).expanduser() if out_path else _default_out_path(
        prov.default_extension)
    out.parent.mkdir(parents=True, exist_ok=True)
    return prov.synthesize(text, voice, out)


# ---- messaging-gateway hook ---------------------------------------------------

def preprocess_voice_note(audio_path: str | Path) -> str:
    """Transcribe a downloaded voice-note file for the messaging gateway.

    Channels (Telegram, WhatsApp bridge, …) download voice notes
    themselves, then call this *before* constructing ``InboundMessage``::

        text = preprocess_voice_note(downloaded_path)
        msg = InboundMessage(channel=..., peer_id=..., peer_name=...,
                             text=text, ...)

    Never raises on provider problems: returns ``"ERROR: …"`` so the
    gateway can reply with a useful message instead of crashing.
    """
    try:
        text = transcribe(audio_path)
    except VoiceError as exc:
        return f"ERROR: voice note could not be transcribed: {exc}"
    except Exception as exc:  # noqa: BLE001 - gateway must not crash
        return f"ERROR: voice note transcription failed: {exc}"
    if not text.strip():
        return "ERROR: voice note transcription came back empty"
    return text.strip()


# ---- agent tools ---------------------------------------------------------------

def voice_tools(config: dict | None = None) -> list[Tool]:
    """Agent tools: ``transcribe_audio`` and ``speak_text`` (both low risk).

    ``config`` may carry ``"stt"`` / ``"tts"`` provider instances (used by
    tests and custom setups), or ``"stt_prefer"`` / ``"tts_prefer"``.
    The permission gate is honored automatically by ``Tool.run``.
    """
    config = config or {}
    stt_provider: STTProvider | None = config.get("stt")
    tts_provider: TTSProvider | None = config.get("tts")
    stt_prefer: str = config.get("stt_prefer", "auto")
    tts_prefer: str = config.get("tts_prefer", "auto")

    def transcribe_audio(audio_path: str) -> str:
        """Transcribe an audio file (voice note, recording) to text."""
        return transcribe(audio_path, prefer=stt_prefer,
                          provider=stt_provider)

    def speak_text(text: str, voice: str = "",
                   out_path: str = "") -> dict:
        """Speak text aloud, saving an audio file. Returns its path."""
        path = speak(text,
                     voice=voice or None,
                     out_path=out_path or None,
                     prefer=tts_prefer, provider=tts_provider)
        return {"audio_path": str(path), "voice": voice or "default"}

    return [
        Tool(
            name="transcribe_audio",
            description=(
                "Transcribe an audio file (mp3/wav/m4a/ogg/…) to text. "
                "Use for voice notes and recordings the user shares."
            ),
            json_schema={
                "type": "object",
                "properties": {"audio_path": {"type": "string"}},
                "required": ["audio_path"],
            },
            risk="low",
            func=transcribe_audio,
        ),
        Tool(
            name="speak_text",
            description=(
                "Convert text to speech and save it as an audio file. "
                "Returns the file path; send it back as a voice note."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {"type": "string"},
                    "out_path": {"type": "string"},
                },
                "required": ["text"],
            },
            risk="low",
            func=speak_text,
        ),
    ]
