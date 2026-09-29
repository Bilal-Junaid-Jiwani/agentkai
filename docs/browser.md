# Browser

The agent drives a **real Chromium** (via Playwright) — not a text
scraper. It can navigate, read rendered pages, click, fill forms,
take screenshots, go back, and download files. Logins persist in the
browser profile, so signing in once keeps working across runs.

## Tools

| Tool | What it does |
|---|---|
| `browser_goto` | Navigate to a URL (http/https only; credentials-in-URL rejected) |
| `browser_read` | Read the rendered page as text |
| `browser_click` | Click an element (by selector or visible text) |
| `browser_fill` | Fill a form field |
| `browser_screenshot` | Capture a screenshot to a file |
| `browser_back` | Go back in history |
| `browser_download` | Download a file to a chosen directory (no path collisions) |

The session starts lazily on first use and stays alive for the run.
Pass `headless=False` when constructing a `BrowserSession` in code to
watch the agent work in a visible window.

## Setup

Playwright needs its Chromium build:

```bash
pip install playwright
playwright install chromium
```

Without it, browser tools raise a clear `BrowserUnavailable` error —
they never pretend to work.

## Safety notes

- Only `http`/`https` URLs; anything else is rejected.
- Downloads land in a directory you choose; filenames are de-duplicated,
  never overwritten silently.
- Like every tool, browser actions go through the permission gate:
  navigation/reading are low risk, form fills and downloads ask first.
- The agent does not bypass CAPTCHAs or access controls for you, and it
  won't exfiltrate credentials — login cookies stay in the local profile
  directory (`~/.agentkai/browser-profile/`).

## Limits

- JavaScript-heavy single-page apps sometimes need a wait/retry; the
  tools surface timeouts as errors rather than hanging.
- No built-in proxy support in v1; the browser uses your machine's
  network directly.
