"""Tests for agentkai.media: honest provider plumbing, no network."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agentkai import media
from agentkai.media import (
    LocalSDProvider,
    MediaError,
    MediaResult,
    OpenAIImagesProvider,
    VideoProvider,
    edit_image,
    generate_image,
    generate_video,
    media_tools,
    register_video_provider,
    select_image_provider,
    select_video_provider,
)
from agentkai.tools import ApprovalRequired, PermissionDenied

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate AGENTKAI_HOME and strip provider env vars."""
    monkeypatch.setenv("AGENTKAI_HOME", str(tmp_path))
    for var in ("OPENAI_API_KEY", "AGENTKAI_IMAGE_PROVIDER",
                "AGENTKAI_VIDEO_PROVIDER", "AGENTKAI_SD_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(LocalSDProvider, "is_available",
                        lambda self, timeout=3: False)
    return tmp_path


def _cfg(home):
    return {"home": str(home)}


# ---- provider selection -------------------------------------------------------

def test_select_explicit_openai_without_key_errors(home):
    with pytest.raises(MediaError, match="no API key"):
        select_image_provider({**_cfg(home),
                               "media": {"image_provider": "openai"}})


def test_select_unknown_provider_errors(home):
    with pytest.raises(MediaError, match="unknown image provider"):
        select_image_provider({**_cfg(home),
                               "media": {"image_provider": "dalle99"}})


def test_select_no_provider_configured_is_honest(home):
    with pytest.raises(MediaError, match="no image provider configured"):
        select_image_provider(_cfg(home), probe_local=False)


def test_select_openai_via_env_key(home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    prov = select_image_provider(_cfg(home), probe_local=False)
    assert isinstance(prov, OpenAIImagesProvider)


def test_select_local_sd_explicit(home):
    prov = select_image_provider(
        {**_cfg(home), "media": {"image_provider": "local_sd"}})
    assert isinstance(prov, LocalSDProvider)


def test_env_provider_selection_beats_auto(home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENTKAI_IMAGE_PROVIDER", "local_sd")
    prov = select_image_provider(_cfg(home), probe_local=False)
    assert isinstance(prov, LocalSDProvider)


# ---- OpenAI image generation (fake transport) -----------------------------------

def _fake_openai_ok(url, payload, headers=None, timeout=30):
    assert "api.openai.com" in url
    assert payload["model"] == "gpt-image-1"
    return {"data": [{"b64_json": PNG_B64}]}


def test_openai_generate_writes_file(home):
    prov = OpenAIImagesProvider("sk-test", request=_fake_openai_ok)
    out = home / "media" / "t.png"
    res = generate_image("a red circle", size="1024x1024",
                         out_path=str(out), config=_cfg(home),
                         provider=prov)
    assert res["path"] == str(out)
    assert res["provider"] == "openai"
    assert Path(res["path"]).read_bytes() == PNG_BYTES


def test_openai_generate_unique_default_path(home):
    prov = OpenAIImagesProvider("sk-test", request=_fake_openai_ok)
    a = generate_image("a red circle", config=_cfg(home), provider=prov)
    b = generate_image("a red circle", config=_cfg(home), provider=prov)
    assert a["path"] != b["path"]
    assert Path(a["path"]).parent.name == "media"


def test_openai_generate_bad_response_is_error_string(home):
    prov = OpenAIImagesProvider(
        "sk-test", request=lambda *a, **k: {"weird": True})
    res = generate_image("x", config=_cfg(home), provider=prov)
    assert isinstance(res, str) and res.startswith("ERROR:")


def test_generate_image_empty_prompt(home):
    assert generate_image("  ", config=_cfg(home)).startswith("ERROR:")


def test_generate_image_bad_size(home):
    assert generate_image("x", size="banana",
                          config=_cfg(home)).startswith("ERROR:")


def test_openai_size_snapping():
    assert media._openai_size(2000, 1000) == "1536x1024"
    assert media._openai_size(500, 900) == "1024x1536"
    assert media._openai_size(800, 800) == "1024x1024"


# ---- OpenAI edit (fake multipart) -------------------------------------------------

def test_openai_edit_writes_file(home, monkeypatch):
    seen = {}

    def fake_multipart(url, fields, files, headers=None, timeout=30):
        seen["fields"] = fields
        seen["files"] = sorted(files)
        assert "api.openai.com" in url
        return {"data": [{"b64_json": PNG_B64}]}

    monkeypatch.setattr(media, "_post_multipart", fake_multipart)
    src = home / "src.png"
    src.write_bytes(PNG_BYTES)
    prov = OpenAIImagesProvider("sk-test", request=_fake_openai_ok)
    res = edit_image(str(src), "make it blue", config=_cfg(home),
                     provider=prov)
    assert res["provider"] == "openai"
    assert Path(res["path"]).read_bytes() == PNG_BYTES
    assert seen["fields"]["prompt"] == "make it blue"
    assert seen["files"] == ["image"]


def test_openai_edit_with_mask(home, monkeypatch):
    seen = {}

    def fake_multipart(url, fields, files, headers=None, timeout=30):
        seen["files"] = sorted(files)
        return {"data": [{"b64_json": PNG_B64}]}

    monkeypatch.setattr(media, "_post_multipart", fake_multipart)
    src = home / "src.png"
    src.write_bytes(PNG_BYTES)
    mask = home / "mask.png"
    mask.write_bytes(PNG_BYTES)
    prov = OpenAIImagesProvider("sk-test", request=_fake_openai_ok)
    res = edit_image(str(src), "inpaint", mask=str(mask),
                     config=_cfg(home), provider=prov)
    assert seen["files"] == ["image", "mask"]
    assert res["path"].endswith(".png")


def test_edit_missing_image_is_error(home):
    res = edit_image(str(home / "nope.png"), "x", config=_cfg(home))
    assert res.startswith("ERROR:")


# ---- local SD ---------------------------------------------------------------------

def test_local_sd_unreachable_is_honest(home):
    prov = LocalSDProvider("http://127.0.0.1:7860")
    with pytest.raises(MediaError, match="not reachable"):
        prov.generate("x", 512, 512, home / "o.png")


def test_local_sd_generate_with_fake_request(home, monkeypatch):
    monkeypatch.setattr(LocalSDProvider, "is_available",
                        lambda self, timeout=3: True)

    def fake(url, payload, headers=None, timeout=600):
        assert url.endswith("/sdapi/v1/txt2img")
        assert payload["width"] == 512 and payload["height"] == 768
        return {"images": [PNG_B64]}

    prov = LocalSDProvider(request=fake)
    res = generate_image("x", size="512x768", config=_cfg(home),
                         provider=prov)
    assert res["provider"] == "local_sd"
    assert (res["width"], res["height"]) == (512, 768)
    assert Path(res["path"]).read_bytes() == PNG_BYTES


def test_local_sd_edit_unsupported(home):
    prov = LocalSDProvider()
    with pytest.raises(MediaError, match="not supported"):
        prov.edit(Path("a.png"), None, "x", 1, 1, Path("b.png"))


# ---- video (honest stub) ------------------------------------------------------------

def test_generate_video_without_provider_is_honest_error(home):
    res = generate_video("a cat", duration=5, config=_cfg(home))
    assert isinstance(res, str)
    assert res.startswith("ERROR:")
    assert "experimental" in res.lower()


def test_select_video_provider_unknown_name_errors(home):
    with pytest.raises(MediaError, match="not registered"):
        select_video_provider({**_cfg(home),
                               "media": {"video_provider": "sora99"}})


def test_video_provider_registration_roundtrip(home):
    class FakeVideo(VideoProvider):
        name = "faketestvideo"

        def generate(self, prompt, duration, out_path):
            out_path.write_bytes(b"FAKEVIDEO")
            return MediaResult(str(out_path), self.name, 640, 360, 9)

    saved = dict(media._VIDEO_PROVIDERS)
    try:
        register_video_provider(FakeVideo())
        res = generate_video("a cat", duration=3, config=_cfg(home))
        assert res["provider"] == "faketestvideo"
        assert Path(res["path"]).read_bytes() == b"FAKEVIDEO"
    finally:
        media._VIDEO_PROVIDERS.clear()
        media._VIDEO_PROVIDERS.update(saved)


# ---- agent tools ----------------------------------------------------------------------

def test_media_tools_shape_and_risk():
    tools = media_tools()
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"generate_image", "generate_video", "edit_image"}
    for t in tools:
        assert t.risk == "medium"
        assert t.json_schema["type"] == "object"
        assert "prompt" in t.json_schema["properties"]


def test_tool_descriptions_carry_cost_warnings():
    by_name = {t.name: t for t in media_tools()}
    assert "COSTS MONEY" in by_name["generate_image"].description
    assert "paid" in by_name["generate_image"].description.lower()
    assert "COSTS MONEY" in by_name["edit_image"].description
    assert "EXPERIMENTAL" in by_name["generate_video"].description


def test_tools_honor_gate_protocol():
    tools = {t.name: t for t in media_tools()}
    deny = lambda action: "deny"  # noqa: E731
    ask = lambda action: "ask"  # noqa: E731
    for tool in tools.values():
        with pytest.raises(PermissionDenied):
            tool.run(_gate=deny, prompt="x")
        with pytest.raises(ApprovalRequired):
            tool.run(_gate=ask, prompt="x")


def test_tools_return_error_strings_not_exceptions(home):
    tools = {t.name: t for t in media_tools(_cfg(home))}
    res = tools["generate_image"].run(prompt="x")  # no provider configured
    assert isinstance(res, str) and res.startswith("ERROR:")
    res = tools["generate_video"].run(prompt="x")
    assert isinstance(res, str) and res.startswith("ERROR:")


def test_skill_tools_py_returns_same_tools(home):
    import importlib.util
    path = (Path(__file__).resolve().parent.parent / "src" / "agentkai"
            / "skills_bundle" / "media" / "tools.py")
    spec = importlib.util.spec_from_file_location("media_skill_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = module.get_tools(_cfg(home))
    assert {t.name for t in tools} == {"generate_image", "generate_video",
                                       "edit_image"}


def test_skill_md_parses():
    from agentkai.skills import parse_skill_md
    from pathlib import Path as P
    meta = parse_skill_md(
        P(__file__).resolve().parent.parent / "src" / "agentkai"
        / "skills_bundle" / "media" / "SKILL.md")
    assert meta["name"] == "media"
    assert "paid" in meta["instructions"].lower()
