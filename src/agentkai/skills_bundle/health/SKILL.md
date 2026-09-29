---
name: health
description: Summarize health trends from user-exported Apple HealthKit / Google Health Connect files (steps, sleep, workouts). File-import only — no device sync.
version: 0.1.0
when: health, steps, sleep, workout, workouts, exercise, fitness, activity, calories
---

# Health

Summarizes health data the **user exports** into `~/.agentkai/health/`
(or `$AGENTKAI_HOME/health/`). There is **no direct device sync** —
Apple and Google don't offer a server API for personal HealthKit/Health
Connect data, so file import is the honest mechanism.

Tools:

- `health_summary(days)` — average daily steps, total workouts, average
  sleep hours over the last N days, plus which files were read.
- `health_workouts(limit)` — most recent workouts (type, duration,
  calories, date).

## Setup (how to get your data in)

**iPhone (Apple Health):** Health app → profile picture → *Export All
Health Data* → unzip → copy `export.xml` into `~/.agentkai/health/`.
(Exports can be hundreds of MB; the tool streams the XML and caps
records, so it's slow but safe.)

**Android (Health Connect):** Health Connect app → *Export data* (or a
companion app that exports CSV) → place files in `~/.agentkai/health/`.

**Manual CSVs** (any source) — the tool also reads:

- `steps.csv`: columns `date,steps` (date as `YYYY-MM-DD`)
- `sleep.csv`: columns `date,hours` (or `start,end` datetimes)
- `workouts.csv`: columns `date,type,duration_min,calories`

Column names are matched case-insensitively; extra columns are ignored.

## Usage notes

- Summaries are informational, not medical advice — say so when asked
  about health implications.
- Health data is sensitive: never copy it into logs, tickets, or
  messages without the user's explicit ask.

## What's real / what's not

- REAL: parsing and summarizing the export files you place in the
  health directory.
- NOT PROVIDED: live sync from phone/watch, background monitoring,
  medical interpretation. If the directory is empty the tools say so
  instead of inventing numbers.
