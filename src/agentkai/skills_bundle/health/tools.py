"""Health skill tools: summarize user-exported HealthKit/Health Connect files.

File-import only — no device sync exists. Reads ~/.agentkai/health/:
  - export.xml  (Apple HealthKit export; streamed, record-capped)
  - steps.csv / sleep.csv / workouts.csv (simple manual exports)
"""
from __future__ import annotations

import codecs
import csv
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentkai.skills_bundle._common import home_dir
from agentkai.tools import Tool

MAX_XML_RECORDS = 200_000  # safety cap for huge HealthKit exports

# A DOCTYPE can only appear in the XML prolog, before the root element;
# a real Apple Health export's prolog is one short line, so scanning
# the first 64 KiB per read up to 1 MiB of prolog is ample.
_PROLOG_CHUNK = 65536
_PROLOG_MAX = 1 << 20

_STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
_SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_ASLEEP_VALUES = {"HKCategoryValueSleepAnalysisAsleep",
                  "HKCategoryValueSleepAnalysisAsleepCore",
                  "HKCategoryValueSleepAnalysisAsleepDeep",
                  "HKCategoryValueSleepAnalysisAsleepREM",
                  "HKCategoryValueSleepAnalysisAsleepUnspecified"}

_WORKOUT_LABELS = {
    "HKWorkoutActivityTypeRunning": "run",
    "HKWorkoutActivityTypeWalking": "walk",
    "HKWorkoutActivityTypeCycling": "cycle",
    "HKWorkoutActivityTypeSwimming": "swim",
    "HKWorkoutActivityTypeYoga": "yoga",
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "strength",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "strength",
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": "hiit",
}


def _health_dir(config: dict | None) -> Path:
    return home_dir(config) / "health"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _read_csv_rows(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = []
            for row in reader:
                rows.append({(k or "").strip().lower(): (v or "").strip()
                             for k, v in row.items()})
            return rows
    except OSError:
        return []


def _prolog_has_doctype(text: str) -> bool | None:
    """Scan an XML prolog prefix for a DOCTYPE declaration.

    Returns True if a DOCTYPE is present, False once the root element
    starts (nothing after it can be a DOCTYPE), None if the prefix
    ends mid-construct and more data is needed to decide. The XML
    declaration, processing instructions and comments are skipped
    the way the parser skips them; any other ``<!`` markup in the
    prolog is a DOCTYPE (or malformed input), so it refuses too.
    A lone trailing ``<`` is undecided — it may be the start of
    anything, split across a chunk boundary.
    """
    i = 0
    n = len(text)
    while True:
        while i < n and text[i] in " \t\r\n\ufeff":
            i += 1
        if i >= n:
            return None
        if i == n - 1 and text[i] == "<":
            return None
        if text.startswith("<?", i):
            end = text.find("?>", i + 2)
            if end < 0:
                return None
            i = end + 2
            continue
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end < 0:
                return None
            i = end + 3
            continue
        if text.startswith("<!", i):
            return True
        if text.startswith("<", i):
            return False
        # Junk before the root element: not a well-formed export;
        # let the parser below report it as usual.
        return False


def _has_doctype(path: Path) -> bool:
    """True if the export declares a DOCTYPE in its prolog.

    Apple Health exports never carry one, and a DOCTYPE is the only
    way to declare entities — while the stdlib parser expands
    internal entities as it reads. A nested-entity ("billion
    laughs") payload in an attribute value would therefore expand
    exponentially in memory before the record cap is ever consulted
    (verified: a three-level entity chain expands 1000x under plain
    ``ET.fromstring``). Only the prolog is scanned, in chunks, so
    the export itself is never loaded whole; UTF-8 and UTF-16 (BOM)
    exports are both handled. A prolog that stays undecided past
    the cap is refused rather than scanned forever.
    """
    try:
        fh = path.open("rb")
    except OSError:
        return False
    with fh:
        encoding = "utf-8"
        text = ""
        total = 0
        while total < _PROLOG_MAX:
            chunk = fh.read(_PROLOG_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if not text:
                if chunk.startswith((codecs.BOM_UTF16_LE,
                                     codecs.BOM_UTF16_BE)):
                    encoding = "utf-16"
            text += chunk.decode(encoding, errors="replace")
            verdict = _prolog_has_doctype(text)
            if verdict is not None:
                return verdict
        return True


def _scan_xml(path: Path) -> tuple[dict[str, float], dict[str, float],
                                   list[dict]]:
    """Stream export.xml -> (steps_by_day, sleep_hours_by_day, workouts)."""
    steps: dict[str, float] = {}
    sleep: dict[str, float] = {}
    workouts: list[dict] = []
    count = 0
    if _has_doctype(path):
        # Refuse the file rather than let a crafted export expand
        # entity payloads in memory while parsing.
        return steps, sleep, workouts
    try:
        context = ET.iterparse(str(path), events=("end",))
    except ET.ParseError:
        return steps, sleep, workouts
    for _event, elem in context:
        tag = elem.tag
        if tag == "Record":
            count += 1
            if count > MAX_XML_RECORDS:
                elem.clear()
                break
            rtype = elem.get("type", "")
            start = _parse_dt(elem.get("startDate"))
            end = _parse_dt(elem.get("endDate"))
            if rtype == _STEP_TYPE and start:
                try:
                    val = float(elem.get("value") or 0)
                except ValueError:
                    val = 0.0
                day = start.date().isoformat()
                steps[day] = steps.get(day, 0.0) + val
            elif rtype == _SLEEP_TYPE and start and end:
                if elem.get("value") in _ASLEEP_VALUES:
                    hours = (end - start).total_seconds() / 3600.0
                    day = end.date().isoformat()  # credit the wake-up day
                    sleep[day] = sleep.get(day, 0.0) + hours
            elem.clear()
        elif tag == "Workout":
            start = _parse_dt(elem.get("startDate"))
            end = _parse_dt(elem.get("endDate"))
            dur_min = None
            if start and end:
                dur_min = round((end - start).total_seconds() / 60.0, 1)
            else:
                try:
                    dur_min = round(float(elem.get("duration") or 0), 1)
                except ValueError:
                    pass
            wtype = elem.get("workoutActivityType", "")
            try:
                cal = float(elem.get("totalEnergyBurned") or 0) or None
            except ValueError:
                cal = None
            workouts.append({
                "date": start.date().isoformat() if start else None,
                "type": _WORKOUT_LABELS.get(wtype,
                                            wtype.replace(
                                                "HKWorkoutActivityType", "")
                                            or None),
                "duration_min": dur_min,
                "calories": round(cal, 1) if cal else None,
                "source": "apple_health",
            })
            elem.clear()
    return steps, sleep, workouts


def _load_all(config: dict | None) -> dict:
    """Merge XML + CSV sources. Returns dict with per-day maps + workouts."""
    hdir = _health_dir(config)
    files_read: list[str] = []
    steps: dict[str, float] = {}
    sleep: dict[str, float] = {}
    workouts: list[dict] = []

    xml_path = hdir / "export.xml"
    if xml_path.exists():
        x_steps, x_sleep, x_workouts = _scan_xml(xml_path)
        for day, val in x_steps.items():
            steps[day] = steps.get(day, 0.0) + val
        for day, val in x_sleep.items():
            sleep[day] = sleep.get(day, 0.0) + val
        workouts.extend(x_workouts)
        files_read.append("export.xml")

    for fname, day_key in (("steps.csv", "steps"), ("sleep.csv", "sleep"),
                           ("workouts.csv", "workouts")):
        path = hdir / fname
        if not path.exists():
            continue
        rows = _read_csv_rows(path)
        if fname == "steps.csv":
            for row in rows:
                day = row.get("date")
                try:
                    val = float(row.get("steps") or 0)
                except ValueError:
                    continue
                if day:
                    steps[day] = steps.get(day, 0.0) + val
        elif fname == "sleep.csv":
            for row in rows:
                day = row.get("date")
                hours = None
                if row.get("hours"):
                    try:
                        hours = float(row["hours"])
                    except ValueError:
                        hours = None
                elif row.get("start") and row.get("end"):
                    s, e = _parse_dt(row["start"]), _parse_dt(row["end"])
                    if s and e:
                        hours = (e - s).total_seconds() / 3600.0
                        day = day or e.date().isoformat()
                if day and hours:
                    sleep[day] = sleep.get(day, 0.0) + hours
        else:  # workouts.csv
            for row in rows:
                try:
                    dur = float(row.get("duration_min") or 0) or None
                except ValueError:
                    dur = None
                try:
                    cal = float(row.get("calories") or 0) or None
                except ValueError:
                    cal = None
                workouts.append({
                    "date": row.get("date") or None,
                    "type": row.get("type") or None,
                    "duration_min": dur,
                    "calories": cal,
                    "source": "csv",
                })
        files_read.append(fname)

    return {"steps": steps, "sleep": sleep, "workouts": workouts,
            "files_read": files_read, "health_dir": str(hdir)}


def _window(days: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat()
            for i in range(max(int(days), 1))]


def get_tools(config: dict | None = None) -> list[Tool]:
    def health_summary(days: int = 30) -> dict | str:
        """Average steps, workouts and sleep over the last N days."""
        data = _load_all(config)
        if not data["files_read"]:
            return (
                "No health data found. Export Apple Health (export.xml) or "
                "Health Connect CSVs into "
                f"{data['health_dir']}/ — see the health SKILL.md for steps.")
        window = _window(days)
        step_vals = [data["steps"].get(d, 0.0) for d in window]
        sleep_vals = [s for d in window
                      if (s := data["sleep"].get(d)) is not None]
        recent_workouts = [w for w in data["workouts"]
                           if w.get("date") and w["date"] in set(window)]
        days_with_steps = sum(1 for v in step_vals if v > 0)
        return {
            "window_days": len(window),
            "avg_daily_steps": (round(sum(step_vals) / days_with_steps, 1)
                                if days_with_steps else 0.0),
            "days_with_step_data": days_with_steps,
            "workouts_in_window": len(recent_workouts),
            "avg_sleep_hours": (round(sum(sleep_vals) / len(sleep_vals), 2)
                                if sleep_vals else None),
            "nights_with_sleep_data": len(sleep_vals),
            "files_read": data["files_read"],
            "note": "Informational only — not medical advice.",
        }

    def health_workouts(limit: int = 20) -> list | str:
        """Most recent workouts from exported health data."""
        data = _load_all(config)
        if not data["files_read"]:
            return (
                "No health data found. Export Apple Health (export.xml) or "
                "Health Connect CSVs into "
                f"{data['health_dir']}/ — see the health SKILL.md for steps.")
        limit = min(max(int(limit), 1), 100)
        workouts = sorted(
            (w for w in data["workouts"] if w.get("date")),
            key=lambda w: w["date"], reverse=True)
        return workouts[:limit]

    return [
        Tool(
            name="health_summary",
            description="Summarize health trends (avg steps, workouts, "
                        "sleep) from user-exported HealthKit/Health "
                        "Connect files in ~/.agentkai/health/. "
                        "File-import only; informational, not medical advice.",
            json_schema={"type": "object",
                         "properties": {"days": {"type": "integer"}},
                         "required": []},
            risk="low", func=health_summary),
        Tool(
            name="health_workouts",
            description="List recent workouts from exported health data "
                        "(Apple export.xml or workouts.csv), newest first.",
            json_schema={"type": "object",
                         "properties": {"limit": {"type": "integer"}},
                         "required": []},
            risk="low", func=health_workouts),
    ]


__all__ = ["get_tools"]
