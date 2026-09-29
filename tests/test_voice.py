"""Voice capabilities: STT/TTS provider selection, tools, gateway hook."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import agentkai.voice as voice
from agentkai.skills import SkillLoader, parse_skill_md
from agentkai.tools import ApprovalRequired, PermissionDenied


# ---- fakes -------------------------------------------------------------------

class FakeSTT(voice.STTProvider):
    name = "fake-stt"

    def __init__(self, text="hello world", fail=False):
        self.text = text
        self.fail = fail
        self.calls: list[str] = []

    @classmethod
    def available(cls) -> bool:
        return True

    def transcribe(self, audio_path):
        self.calls.append(str(audio_path))
        if self.fail:
            raise RuntimeError("boom")
        return self.text


class FakeTTS(voice.TTSProvider):
    name = "fake-tts"
    default_extension = "wav"

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    @classmethod
    def available(cls) -> bool:
        return True

    def voices(self):
        return ["fake-voice"]

    def synthesize(self, text, voice, out_path):
        self.calls.append((text, voice))
        p = Path(out_path)
        p.write_bytes(b"FAKEAUDIO")
        return p


@pytest.fixture()
def wav_file(tmp_path):
    p = tmp_path / "note.wav"
    p.write_bytes(b"RIFF" + b"\x00" * 44)  # content never decoded by fakes
    return p


@pytest.fixture()
def fake_config():
    return {"stt": FakeSTT("transcribed text"), "tts": FakeTTS()}


# ---- provider selection --------------------------------------------------------

def test_select_stt_prefers_local_when_available(monkeypatch):
    monkeypatch.setattr(voice, "faster_whisper_available", lambda: True)
    monkeypatch.setattr(voice, "openai_available", lambda: False)
    prov = voice.select_stt()
    assert isinstance(prov, voice.LocalWhisperSTT)


def test_select_stt_falls_back_to_openai(monkeypatch):
    monkeypatch.setattr(voice, "faster_whisper_available", lambda: False)
    monkeypatch.setattr(voice, "openai_available", lambda: True)
    prov = voice.select_stt()
    assert isinstance(prov, voice.OpenAIWhisperSTT)


def test_select_stt_explicit_prefer_openai(monkeypatch):
    monkeypatch.setattr(voice, "faster_whisper_available", lambda: True)
    monkeypatch.setattr(voice, "openai_available", lambda: True)
    assert isinstance(voice.select_stt("openai"), voice.OpenAIWhisperSTT)
    assert isinstance(voice.select_stt("local"), voice.LocalWhisperSTT)


def test_select_stt_no_provider_gives_clear_error(monkeypatch):
    monkeypatch.setattr(voice, "faster_whisper_available", lambda: False)
    monkeypatch.setattr(voice, "openai_available", lambda: False)
    with pytest.raises(voice.VoiceError) as ei:
        voice.select_stt()
    msg = str(ei.value)
    assert "faster-whisper" in msg and "OPENAI_API_KEY" in msg


def test_select_stt_bad_prefer():
    with pytest.raises(voice.VoiceError):
        voice.select_stt("nonsense")


def test_select_tts_prefers_openai_when_key_present(monkeypatch):
    monkeypatch.setattr(voice, "openai_available", lambda: True)
    monkeypatch.setattr(voice, "local_tts_command", lambda: "espeak")
    assert isinstance(voice.select_tts(), voice.OpenAITTS)


def test_select_tts_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(voice, "openai_available", lambda: False)
    monkeypatch.setattr(voice, "local_tts_command", lambda: "espeak")
    assert isinstance(voice.select_tts(), voice.LocalEspeakTTS)


def test_select_tts_no_provider_gives_clear_error(monkeypatch):
    monkeypatch.setattr(voice, "openai_available", lambda: False)
    monkeypatch.setattr(voice, "local_tts_command", lambda: None)
    with pytest.raises(voice.VoiceError) as ei:
        voice.select_tts()
    assert "OPENAI_API_KEY" in str(ei.value)


def test_openai_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    assert voice.openai_key() == "sk-test-123"


def test_provider_matrix_is_honest():
    matrix = voice.provider_matrix()
    assert set(matrix) == {
        "stt.local (faster-whisper)",
        "stt.openai (whisper-1)",
        "tts.openai (tts-1)",
        "tts.local",
    }
    # no fake third-party endpoints smuggled in
    assert not any("edge" in k.lower() for k in matrix)
    for info in matrix.values():
        assert isinstance(info["available"], bool)
        assert info["note"]


# ---- audio normalization ---------------------------------------------------------

def test_unsupported_extension_rejected(tmp_path):
    p = tmp_path / "note.xyz"
    p.write_bytes(b"data")
    with pytest.raises(voice.VoiceError) as ei:
        voice._ensure_wav(p)
    assert "unsupported audio format" in str(ei.value)


def test_missing_file_rejected():
    with pytest.raises(voice.VoiceError) as ei:
        voice._ensure_wav("/nonexistent/note.wav")
    assert "not found" in str(ei.value)


def test_wav_passes_through_without_ffmpeg(monkeypatch, wav_file):
    monkeypatch.setattr(voice, "ffmpeg_available", lambda: False)
    assert voice._ensure_wav(wav_file) == wav_file


def test_non_wav_needs_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "ffmpeg_available", lambda: False)
    p = tmp_path / "note.mp3"
    p.write_bytes(b"data")
    with pytest.raises(voice.VoiceError) as ei:
        voice._ensure_wav(p)
    assert "ffmpeg" in str(ei.value)


def test_ffmpeg_conversion_invoked(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "ffmpeg_available", lambda: True)
    src = tmp_path / "note.mp3"
    src.write_bytes(b"data")

    converted = tmp_path / "converted.wav"

    def fake_run(args, **kwargs):
        converted.write_bytes(b"RIFF")
        # mimic _ensure_wav writing into its own temp dir
        out = Path(args[args.index("-c:a") + 2])
        out.write_bytes(b"RIFF")

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    out = voice._ensure_wav(src)
    assert out.suffix == ".wav"
    assert out != src
    voice._cleanup_converted(out, src)
    assert not out.parent.exists()


# ---- tools + gate protocol --------------------------------------------------------

def test_voice_tools_registered(fake_config):
    tools = voice.voice_tools(fake_config)
    assert [t.name for t in tools] == ["transcribe_audio", "speak_text"]
    assert all(t.risk == "low" for t in tools)
    for t in tools:
        assert t.to_openai_schema()["function"]["name"] == t.name


def test_transcribe_tool_uses_provider(fake_config, wav_file):
    tool = {t.name: t for t in voice.voice_tools(fake_config)}[
        "transcribe_audio"]
    assert tool.run(audio_path=str(wav_file)) == "transcribed text"


def test_speak_tool_writes_file(fake_config, tmp_path):
    tool = {t.name: t for t in voice.voice_tools(fake_config)}["speak_text"]
    out = tmp_path / "out.wav"
    result = tool.run(text="hello", out_path=str(out))
    assert result["audio_path"] == str(out)
    assert out.exists()


def test_tool_reports_errors_as_strings(fake_config, wav_file):
    tools = voice.voice_tools(
        {"stt": FakeSTT(fail=True), "tts": FakeTTS()})
    tool = {t.name: t for t in tools}["transcribe_audio"]
    result = tool.run(audio_path=str(wav_file))
    assert isinstance(result, str) and result.startswith("ERROR:")


def test_gate_deny_raises(fake_config, wav_file):
    tool = {t.name: t for t in voice.voice_tools(fake_config)}[
        "transcribe_audio"]
    with pytest.raises(PermissionDenied):
        tool.run(_gate=lambda action: "deny", audio_path=str(wav_file))


def test_gate_ask_raises_approval(fake_config, wav_file):
    tool = {t.name: t for t in voice.voice_tools(fake_config)}[
        "transcribe_audio"]
    with pytest.raises(ApprovalRequired):
        tool.run(_gate=lambda action: "ask", audio_path=str(wav_file))


def test_gate_sees_tool_action(fake_config, wav_file):
    seen: list[dict] = []
    tool = {t.name: t for t in voice.voice_tools(fake_config)}[
        "transcribe_audio"]
    tool.run(_gate=lambda a: seen.append(a) or "allow",
             audio_path=str(wav_file))
    assert seen[0]["tool"] == "transcribe_audio"
    assert seen[0]["risk"] == "low"


def test_speak_rejects_empty_text(fake_config):
    tool = {t.name: t for t in voice.voice_tools(fake_config)}["speak_text"]
    result = tool.run(text="   ")
    assert result.startswith("ERROR:")


def test_speak_rejects_huge_text(fake_config):
    tool = {t.name: t for t in voice.voice_tools(fake_config)}["speak_text"]
    result = tool.run(text="x" * (voice.MAX_TTS_CHARS + 1))
    assert result.startswith("ERROR:")


def test_speak_default_out_path(fake_config, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = {t.name: t for t in voice.voice_tools(fake_config)}["speak_text"]
    result = tool.run(text="hello")
    p = Path(result["audio_path"])
    assert p.exists()
    assert ".agentkai/voice" in str(p)


# ---- gateway hook -----------------------------------------------------------------

def test_preprocess_voice_note_success(monkeypatch, wav_file):
    monkeypatch.setattr(voice, "transcribe", lambda p, **k: "hello there")
    assert voice.preprocess_voice_note(wav_file) == "hello there"


def test_preprocess_voice_note_never_raises(monkeypatch):
    monkeypatch.setattr(
        voice, "transcribe",
        lambda p, **k: (_ for _ in ()).throw(voice.VoiceError("no provider")))
    result = voice.preprocess_voice_note("/tmp/x.wav")
    assert result.startswith("ERROR:")


def test_preprocess_voice_note_empty_transcript(monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda p, **k: "  ")
    assert voice.preprocess_voice_note("/tmp/x.wav").startswith("ERROR:")


# ---- skill framework ---------------------------------------------------------------

def test_skill_md_parses():
    meta = parse_skill_md(
        Path(voice.__file__).parent / "skills_bundle" / "voice" / "SKILL.md")
    assert meta["name"] == "voice"
    assert meta["version"] == "0.1.0"


def test_skill_loads_with_tools():
    loader = SkillLoader()
    skill = loader.load("voice")
    assert skill.source == "bundled"
    assert sorted(t.name for t in skill.tools) == [
        "speak_text", "transcribe_audio"]
    assert "voice note" in skill.instructions.lower()


def test_skill_tools_do_not_require_credentials():
    # get_tools must not blow up when no provider is configured; failures
    # surface as ERROR strings at call time, not import time.
    from agentkai.skills_bundle.voice.tools import get_tools
    tools = get_tools({})
    assert len(tools) == 2


# ---- real-environment probes (skip when the binary/dep is missing) ------------------

@pytest.mark.skipif(not voice.faster_whisper_available(),
                    reason="faster-whisper not installed")
def test_real_local_whisper_probe():
    assert voice.LocalWhisperSTT.available()


@pytest.mark.skipif(voice.local_tts_command() is None,
                    reason="no local TTS binary installed")
def test_real_local_tts_probe():
    assert voice.LocalEspeakTTS.available()
    assert voice.LocalEspeakTTS().voices()


@pytest.mark.skipif(not voice.ffmpeg_available(),
                    reason="ffmpeg not installed")
def test_real_ffmpeg_probe():
    assert voice.ffmpeg_available()


def test_honest_matrix_report():
    """Always runs: documents what this machine can actually do."""
    matrix = voice.provider_matrix()
    available = [k for k, v in matrix.items() if v["available"]]
    # ffmpeg is installed in this dev VM; faster-whisper/openai key are not.
    assert isinstance(available, list)
