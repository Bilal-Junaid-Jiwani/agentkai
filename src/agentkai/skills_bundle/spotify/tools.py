"""Spotify skill tools (Spotify Web API)."""
from __future__ import annotations

from typing import Any

from agentkai.skills_bundle._common import (
    auth_error, get_token, test_transport,
)
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

ENV_VAR = "SPOTIFY_TOKEN"
TOKEN_FILE = "spotify_token.json"
BASE = "https://api.spotify.com/v1"


def _client(config: dict | None) -> HttpClient | None:
    token = get_token(config, ENV_VAR, TOKEN_FILE)
    if not token:
        return None
    return HttpClient(BASE, headers={"Authorization": f"Bearer {token}"},
                      transport=test_transport(config))


def _no_auth() -> str:
    return auth_error("spotify", ENV_VAR, TOKEN_FILE)


def _track(item: dict) -> dict:
    return {
        "uri": item.get("uri"),
        "name": item.get("name"),
        "artists": [a.get("name") for a in item.get("artists", []) or []],
        "album": ((item.get("album") or {}).get("name")),
        "duration_ms": item.get("duration_ms"),
        "url": ((item.get("external_urls") or {}).get("spotify")),
    }


def get_tools(config: dict | None = None) -> list[Tool]:
    def spotify_search(query: str, type: str = "track",
                       limit: int = 10) -> dict | str:
        """Search Spotify. type: track|album|artist|playlist (comma-sep ok)."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request("GET", "/search", params={
                "q": query, "type": type,
                "limit": str(min(max(int(limit), 1), 50))})
        except HttpError as exc:
            return f"ERROR: spotify search failed: {exc}"
        data = data or {}
        out: dict[str, Any] = {}
        for kind in ("tracks", "albums", "artists", "playlists"):
            section = data.get(kind, {}) or {}
            items = section.get("items", []) or []
            out[kind] = [
                {"uri": i.get("uri"), "name": i.get("name"),
                 "url": ((i.get("external_urls") or {}).get("spotify"))}
                for i in items if i
            ]
        return out

    def spotify_now_playing() -> dict | str:
        """What's currently playing (track, artists, progress, device)."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request("GET", "/me/player/currently-playing")
        except HttpError as exc:
            if exc.status == 204:
                return {"is_playing": False,
                        "note": "nothing is playing on any device"}
            return f"ERROR: spotify now playing failed: {exc}"
        if not data:
            return {"is_playing": False,
                    "note": "nothing is playing on any device"}
        item = (data or {}).get("item") or {}
        device = (data or {}).get("device") or {}
        return {"is_playing": (data or {}).get("is_playing", False),
                "progress_ms": (data or {}).get("progress_ms"),
                "device": device.get("name"),
                "track": _track(item) if item else None}

    def spotify_play(context_uri: str = "", uris: list[str] | None = None,
                     device_id: str = "") -> dict | str:
        """Resume playback, or play a context/URI list. MEDIUM RISK."""
        client = _client(config)
        if client is None:
            return _no_auth()
        body: dict[str, Any] = {}
        if context_uri:
            body["context_uri"] = context_uri
        if uris:
            body["uris"] = uris
        params = {"device_id": device_id} if device_id else None
        try:
            client.request("PUT", "/me/player/play",
                           params=params, json_body=body or None)
        except HttpError as exc:
            return f"ERROR: spotify play failed: {exc}"
        return {"ok": True}

    def spotify_pause(device_id: str = "") -> dict | str:
        """Pause playback. MEDIUM RISK."""
        client = _client(config)
        if client is None:
            return _no_auth()
        params = {"device_id": device_id} if device_id else None
        try:
            client.request("PUT", "/me/player/pause", params=params)
        except HttpError as exc:
            return f"ERROR: spotify pause failed: {exc}"
        return {"ok": True}

    def spotify_queue(uri: str, device_id: str = "") -> dict | str:
        """Add a track URI to the playback queue. MEDIUM RISK."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not uri.strip():
            return "ERROR: uri is required (e.g. spotify:track:…)"
        params: dict[str, Any] = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        try:
            client.request("POST", "/me/player/queue", params=params)
        except HttpError as exc:
            return f"ERROR: spotify queue failed: {exc}"
        return {"ok": True, "queued": uri}

    return [
        Tool(
            name="spotify_search",
            description=("Search Spotify for tracks, albums, artists or "
                         "playlists. Returns URIs usable with spotify_play / "
                         "spotify_queue."),
            json_schema={"type": "object",
                         "properties": {
                             "query": {"type": "string"},
                             "type": {"type": "string"},
                             "limit": {"type": "integer"}},
                         "required": ["query"]},
            risk="low",
            func=spotify_search,
        ),
        Tool(
            name="spotify_now_playing",
            description=("Show what's currently playing on the user's Spotify "
                         "(track, artists, progress, device)."),
            json_schema={"type": "object", "properties": {}},
            risk="low",
            func=spotify_now_playing,
        ),
        Tool(
            name="spotify_play",
            description=("Resume playback, or play a context URI "
                         "(album/playlist) or a list of track URIs. MEDIUM "
                         "RISK: changes what the user hears."),
            json_schema={"type": "object",
                         "properties": {
                             "context_uri": {"type": "string"},
                             "uris": {"type": "array",
                                      "items": {"type": "string"}},
                             "device_id": {"type": "string"}}},
            risk="medium",
            func=spotify_play,
        ),
        Tool(
            name="spotify_pause",
            description="Pause Spotify playback. MEDIUM RISK.",
            json_schema={"type": "object",
                         "properties": {"device_id": {"type": "string"}}},
            risk="medium",
            func=spotify_pause,
        ),
        Tool(
            name="spotify_queue",
            description=("Add a track URI to the Spotify queue. MEDIUM RISK: "
                         "only queue songs the user asked for."),
            json_schema={"type": "object",
                         "properties": {
                             "uri": {"type": "string"},
                             "device_id": {"type": "string"}},
                         "required": ["uri"]},
            risk="medium",
            func=spotify_queue,
        ),
    ]


__all__ = ["get_tools", "ENV_VAR", "TOKEN_FILE"]
