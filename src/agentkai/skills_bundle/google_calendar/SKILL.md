---
name: google_calendar
description: Read the user's agenda and create, update, or delete calendar events.
version: 0.1.0
when: calendar, schedule, agenda, meeting, event, appointment, reminder
---

# Google Calendar

Thin wrapper over the Google Calendar API v3. Tools: `calendar_list` (low),
`calendar_create` (medium), `calendar_update` (high), `calendar_delete`
(high — destructive).

## Setup (required — nothing works without this)

This skill needs a Google OAuth2 access token with the
`https://www.googleapis.com/auth/calendar` scope. Steps:

1. Go to https://console.cloud.google.com/ → create a project → enable the
   **Google Calendar API**.
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   (Desktop app). Add yourself as a test user under **OAuth consent screen**.
3. Authorize once and copy the **access token**.
4. Provide it as the `GCAL_TOKEN` environment variable, or as
   `~/.agentkai/gcal_token.json` containing `{"access_token": "..."}`.

Access tokens expire (usually ~1 hour); rotate the token file or env var —
the skill reads it fresh on every call. The skill does not do OAuth for you.

## Usage notes

- Times are RFC3339: `2026-10-01T14:00:00+05:00`, or date-only
  `2026-10-01` for all-day events. Always confirm the timezone with the
  user; default is UTC when no offset is given.
- `calendar_create` is medium risk, `calendar_update`/`calendar_delete` are
  high risk and go through the permission gate.
- `calendar_list` defaults to `primary`; pass another calendar ID (email
  address) to read shared calendars you can access.
