# Voice

Speech-to-text and text-to-speech with two honest provider tiers.
Nothing is faked: if no provider is available, the tools return a clear
error saying exactly what to install or configure.

## Providers

**Speech-to-text** (`transcribe_audio`), auto-selected in order:

1. `local` — faster-whisper on your machine. Offline, private, audio
   never leaves the box. Optional dependency: `pip install agentkai[voice]`
   (downloads a ~140 MB `base` model on first use).
2. `openai` — OpenAI Whisper API. Needs `OPENAI_API_KEY`; audio leaves
   the machine.

**Text-to-speech** (`speak_text`), auto-selected in order:

1. `openai` — OpenAI TTS (`tts-1`; voices alloy, echo, fable, onyx, nova,
   shimmer). Needs `OPENAI_API_KEY`. Natural-sounding.
2. `local` — `espeak`/`espeak-ng` on Linux, `say` on macOS. Free and
   offline, but robotic.

There is deliberately no free third-party TTS endpoint: undocumented
scraped endpoints break without notice and would be a dishonest
dependency.

Run `provider_matrix()` in `agentkai.voice` (or ask the agent) to see
what works on your machine right now.

## Setup

```bash
pip install agentkai[voice]   # offline STT (faster-whisper)
sudo apt install espeak-ng    # offline TTS (Linux); macOS ships `say`
# sudo apt install ffmpeg     # for non-wav audio input
export OPENAI_API_KEY=...     # API tiers (or ~/.agentkai/openai_token.json)
```

## Audio formats

mp3, wav, m4a, ogg/oga, opus, flac, webm, aac. Non-wav input is converted
to 16 kHz mono wav via ffmpeg when available; otherwise the tool errors
clearly.

## Voice notes in chat

Messaging channels download incoming voice-note files and transcribe them
before the agent sees the message. Transcription failures come back as
readable `ERROR: …` strings so the gateway replies usefully instead of
crashing.

## What's not here

- No real-time voice conversation / telephony in v1 — transcription and
  file-based synthesis only.
- No speaker identification.
