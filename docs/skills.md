# Skills

A skill is a reusable playbook: a `SKILL.md` file (YAML frontmatter —
name, description, version, when-to-use — plus markdown instructions the
agent reads) with an optional `tools.py` exposing `get_tools(config)`.
Skills teach the agent *how* to do a domain well; tools give it the
*hands*.

```bash
agentkai skills list          # bundled + your installed skills
agentkai skills show gmail    # read a skill's instructions
agentkai skills install ./my-skill
agentkai skills install owner/repo --name my-skill
agentkai skills remove my-skill
```

Installs go to `~/.agentkai/skills/`. Bundled skills ship with the
package and can't be removed.

## Bundled skill catalog

Honest status per skill — what works, what you must configure, and
what's deliberately not there. Full setup steps are in each skill's
SKILL.md (`agentkai skills show <name>`).

| Skill | What it does | Setup |
|---|---|---|
| `gmail` | Search, read, draft, **send** Gmail | Google OAuth token (`GMAIL_TOKEN` or `~/.agentkai/gmail_token.json`); sending is high-risk/gated |
| `google_calendar` | List/create/update/delete events | Google OAuth token (`GCAL_TOKEN` or `~/.agentkai/gcal_token.json`) |
| `github` | Repos, issues, PRs, Actions runs | Personal access token (`GITHUB_TOKEN` or `~/.agentkai/github_token.json`) |
| `spotify` | Search, now-playing, play/pause/queue | User OAuth token (`SPOTIFY_TOKEN`), needs an active device for playback |
| `places` | Place search + details (hours, phone) via OpenStreetMap | none — keyless |
| `image_search` | Keyword image search (Openverse, openly-licensed) | none for basic use |
| `voice` | Transcribe audio, speak text | faster-whisper (`pip install agentkai[voice]`) or `OPENAI_API_KEY`; espeak for offline TTS |
| `media` | Generate/edit images | `OPENAI_API_KEY` (paid) **or** local Stable Diffusion web UI with `--api` |
| `payments` | Stripe payment intents | `STRIPE_SECRET_KEY`; all tools high-risk, every charge needs approval |
| `shopping` | Product search | groceries via OpenFoodFacts work out of the box; general shopping needs a provider you plug in |
| `travel` | Live flight tracking (OpenSky) | tracking works keyless; flight/hotel *search* needs a provider you plug in |
| `health` | Summarize exported HealthKit/Health Connect data | drop `export.xml`/CSVs in `~/.agentkai/health/` — file import only, no device sync |

### Read the fine print

- **OAuth tokens expire** (usually ~1 hour). Gmail, Calendar, and Spotify
  skills read the token fresh on every call, so rotate the file/env var —
  but AgentKai does **not** do the OAuth dance for you. Long-lived setups
  need a refresh-token flow you run yourself.
- **Paid APIs cost real money.** `media` (OpenAI images) and any LLM
  provider bill your key per call; the tools say so and ask first.
- **Pluggable, not magic.** `shopping` and `travel` search need a
  provider *you* register (see their SKILL.md for the 10-line pattern).
  Only OpenFoodFacts groceries and OpenSky flight tracking are bundled
  and real.
- **Health is file-import only.** Apple/Google offer no server API for
  personal health data, so you export files yourself. No live sync, no
  medical advice.
- **Payments never touch card numbers.** Stripe intents only; card entry
  happens in Stripe's hosted UI. Use test keys (`sk_test_…`) until real
  money is truly intended.

## Writing a skill

```
my-skill/
  SKILL.md     # frontmatter: name, description, version, when
  tools.py     # def get_tools(config) -> list[Tool]  (optional)
```

```markdown
---
name: my-skill
description: What it does, in one line.
version: 0.1.0
when: keywords, that, trigger, this, skill
---

# My skill

Instructions the agent follows when this skill is active...
```

Keep instructions procedural and concrete — checklists beat essays.
Install with `agentkai skills install ./my-skill` and iterate.
