---
name: voice
description: Speech-to-text and text-to-speech. Transcribe voice notes and recordings; speak text aloud as audio files.
version: 0.1.0
when: voice, audio, speech, transcribe, transcription, voice note, tts, speak, say aloud, dictate
---

# Voice

Speech-to-text (`transcribe_audio`) and text-to-speech (`speak_text`).
Both are **low risk** and work through the normal permission gate.

## Providers (honest — nothing is faked)

Run `provider_matrix()` in `agentkai.voice` to see what works on this machine.

**Speech-to-text**, auto-selected in this order:
1. `local-whisper` — faster-whisper on this machine. Offline, private, audio
   never leaves the box. Optional: `pip install agentkai[voice]`.
2. `openai-whisper` — OpenAI Whisper API. Needs `OPENAI_API_KEY`.

**Text-to-speech**, auto-selected in this order:
1. `openai-tts` — OpenAI TTS API (`tts-1`, voices: alloy, echo, fable, onyx,
   nova, shimmer). Needs `OPENAI_API_KEY`. Natural-sounding.
2. `local-tts` — `espeak`/`espeak-ng` on Linux, `say` on macOS. Free and
   offline, but robotic.

There is deliberately **no** free third-party TTS endpoint (e.g. scraped
Edge-TTS): those are undocumented, break without notice, and would be a
dishonest dependency. If neither tier is available the tools return a clear
`ERROR:` explaining exactly what to install or configure.

## Audio formats

mp3, wav, m4a, ogg/oga, opus, flac, webm, aac. Anything that isn't wav is
converted to 16 kHz mono wav with ffmpeg when ffmpeg is installed;
otherwise the tool errors with a clear message.

## Voice notes in chat

Channels (Telegram, WhatsApp bridge, …) download voice-note files
themselves and must call `agentkai.voice.preprocess_voice_note(path)`
*before* constructing their `InboundMessage`, putting the returned text in
`msg.text`. Transcription failures come back as `ERROR: …` strings so the
gateway can reply usefully instead of crashing.

## Setup

- Offline STT: `pip install agentkai[voice]` (faster-whisper; downloads a
  ~140 MB `base` model on first use).
- API tiers: set `OPENAI_API_KEY`, or create `~/.agentkai/openai_token.json`
  containing `{"access_token": "<key>"}`. The key is never logged.
- Local TTS: `sudo apt install espeak-ng` (Linux). macOS ships `say`.
- Format conversion: install `ffmpeg` for non-wav input.

## Usage notes

- `speak_text` caps input at 4000 characters — split longer text first.
- `speak_text` returns `{"audio_path": …}`; on chat channels, send that
  file back as a voice note / audio attachment.
- Transcription is best-effort on noisy audio; ask the user to repeat when
  the transcript looks wrong rather than acting on garbage.
