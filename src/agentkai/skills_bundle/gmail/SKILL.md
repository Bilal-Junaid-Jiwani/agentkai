---
name: gmail
description: Search, read, draft, and send email through the user's Gmail account.
version: 0.1.0
when: email, gmail, inbox, send an email, read email, draft, mailbox
---

# Gmail

Thin wrapper over the Gmail REST API. Tools: `gmail_search` (low risk),
`gmail_read_thread` (low), `gmail_create_draft` (medium),
`gmail_send` (high — actually delivers mail).

## Setup (required — nothing works without this)

This skill needs a Google OAuth2 access token with the
`https://www.googleapis.com/auth/gmail.modify` scope. Steps:

1. Go to https://console.cloud.google.com/ → create a project → enable the
   **Gmail API**.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   (Desktop app). Add yourself as a test user under **OAuth consent screen**.
3. Authorize once (any OAuth playground / `gcloud auth` flow works) and copy
   the **access token**.
4. Provide it to the agent either as the `GMAIL_TOKEN` environment variable,
   or as `~/.agentkai/gmail_token.json` containing:
   `{"access_token": "..."}`

Access tokens expire (usually after ~1 hour). For long-lived use, implement a
refresh-token flow and rewrite the token file — the skill reads the file fresh
on every call, so rotating it works without a restart. The skill does not
perform the OAuth dance for you.

## Usage notes

- `gmail_search` uses Gmail's query syntax (`from:`, `is:unread`,
  `after:2026/01/01`, …). It returns message/thread IDs; call
  `gmail_read_thread` to read one.
- `gmail_send` delivers immediately and is **high risk**: it always goes
  through the permission gate. Prefer `gmail_create_draft` when unsure.
- Never invent recipients: only send to addresses the user explicitly gave.
