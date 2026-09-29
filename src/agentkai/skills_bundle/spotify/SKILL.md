---
name: spotify
description: Search Spotify, see what's playing, and control playback (play/pause/queue).
version: 0.1.0
when: spotify, music, song, play music, now playing, playlist, queue a song
---

# Spotify

Thin wrapper over the Spotify Web API. `spotify_search` and
`spotify_now_playing` are low risk; `spotify_play`, `spotify_pause` and
`spotify_queue` are medium risk (they change what the user hears) and go
through the permission gate.

## Setup (required — nothing works without this)

Playback control needs a **user** OAuth token (not a client-credentials
token) with scopes `user-read-playback-state` and
`user-modify-playback-state`. Steps:

1. Go to https://developer.spotify.com/dashboard → create an app.
2. Authorize with the two scopes above (Spotify's Authorization Code flow —
   any OAuth helper works) and copy the **access token**.
3. Provide it as the `SPOTIFY_TOKEN` environment variable, or as
   `~/.agentkai/spotify_token.json` containing `{"access_token": "..."}`.

Access tokens expire after ~1 hour; rotate the token file or env var — the
skill reads it fresh on every call. The skill does not do OAuth for you,
and search/playback will not work without a valid user token.

## Usage notes

- Playback commands target the user's **active device**. If nothing is
  playing, `spotify_now_playing` returns `is_playing: false` — start
  playback from a Spotify app first, or pass a `device_id`.
- `spotify_play` with no arguments resumes; with `context_uri` it plays a
  track/album/playlist URI (`spotify:track:…`); with `uris` it plays a list.
- Queueing a song the user didn't ask for is rude — always confirm first.
