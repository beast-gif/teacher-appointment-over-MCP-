"""Step 5: is a faculty member free at a given time?

This is the logic the MCP tools will call. For now it reads the Excel file
directly; later it will read from the database instead.

Run:
    python -m app.availability EAC_7th_sem_timetable.xlsx "Bhavana ma'am" Monday 10:00
    python -m app.availability EAC_7th_sem_timetable.xlsx "sushma" 2026-10-02 2pm
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import openpyxl
from rapidfuzz import fuzz

from .ingestion.extract import extract_sessions
from .ingestion.faculty import build_faculty, faculty_schedule, load_name_fixes
from .ingestion.grid import Grid
from .ingestion.reader import (
    find_days, find_legend, find_time_header, read_header_info, read_lab_slots, read_legend, read_slots,
)
from .ingestion.text import DAY_LABELS, DAY_NAMES, name_key, parse_day

MIN_SLOT_MINUTES = 15


@dataclass
class Timetable:
    people: dict[str, dict]            # key -> {"name", "courses"}
    schedule: dict[str, list[dict]]    # key -> busy / maybe blocks
    breaks: list[dict]                 # {"label", "start", "end"}
    days: set[int]                     # weekdays that have classes
    day_start: time
    day_end: time
    sessions: list[dict]               # every block in the class timetable
    courses: dict[str, dict]           # code -> legend row (title, rooms ...)
    section: dict                      # program, semester, default_room


def load_timetable(path: str) -> Timetable:
    """Steps 2-4 in one call."""
    grid = Grid.from_worksheet(openpyxl.load_workbook(path).worksheets[0])
    day_col, day_rows = find_days(grid)
    header_row = find_time_header(grid, day_col, min(day_rows))
    slots, breaks = read_slots(grid, header_row, day_col)
    labs = read_lab_slots(grid, header_row, min(day_rows), day_col)
    legend = find_legend(grid)
    courses = read_legend(grid, legend) if legend else []
    sessions, _ = extract_sessions(grid, day_rows, slots, labs, courses)
    people, _ = build_faculty(courses, load_name_fixes())
    return Timetable(
        people=people,
        schedule=faculty_schedule(sessions, people),
        breaks=[b for b in breaks if "start" in b],
        days=set(day_rows.values()),
        day_start=min(s["start"] for s in slots),
        day_end=max(s["end"] for s in slots),
        sessions=sessions,
        courses={c["code"]: c for c in courses},
        section=read_header_info(grid, header_row),
    )


# ---------------------------------------------------------------- finding people

POLITE_WORDS = re.compile(r"\b(ma'?am|madam|mam|sir|miss|teacher|faculty)\b", re.I)


def find_faculty(query: str, tt: Timetable) -> dict:
    """'Bhavana ma'am' -> Dr. Bhavana V.

    Returns {"status": "found" | "ambiguous" | "not_found", "matches": [...]}.
    """
    q = name_key(POLITE_WORDS.sub(" ", query))
    if not q:
        return {"status": "not_found", "matches": []}
    scored = sorted(
        ((fuzz.token_set_ratio(q, key), fuzz.ratio(q, key), key) for key in tt.people),
        reverse=True,
    )
    good = [s for s in scored if s[0] >= 75]
    if not good:
        return {"status": "not_found", "matches": []}
    best = good[0]
    close = [s for s in good if best[0] - s[0] <= 5 and best[1] - s[1] <= 15]
    matches = [{"key": k, "name": tt.people[k]["name"], "score": s} for s, _, k in close]
    if len(close) == 1:
        # one candidate: sure match, or a typo worth confirming ("bavana")
        return {"status": "found" if best[0] >= 85 else "did_you_mean", "matches": matches}
    return {"status": "ambiguous", "matches": matches}


# ---------------------------------------------------------------- time helpers

def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _time(m: int) -> time:
    return time(m // 60, m % 60)


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


# ---------------------------------------------------------------- availability

def check_availability(tt: Timetable, key: str, day: int, at: time,
                       duration_min: int = MIN_SLOT_MINUTES) -> dict:
    """Status is one of: free, busy, maybe, break, outside_hours."""
    name = tt.people[key]["name"]
    start, end = _minutes(at), _minutes(at) + duration_min
    result = {"faculty": name, "day": DAY_LABELS[day], "time": at.strftime("%H:%M"),
              "duration_min": duration_min}

    if day not in tt.days or start < _minutes(tt.day_start) or end > _minutes(tt.day_end):
        return {**result, "status": "outside_hours",
                "reason": f"College hours are {tt.day_start:%H:%M}-{tt.day_end:%H:%M}, "
                          f"{', '.join(DAY_LABELS[d] for d in sorted(tt.days))}",
                "alternatives": free_slots(tt, key, day, after=at) if day in tt.days else []}

    blocks = [b for b in tt.schedule[key] if b["day"] == day
              and _overlaps(start, end, _minutes(b["start"]), _minutes(b["end"]))]
    busy = [b for b in blocks if b["status"] == "busy"]
    if busy:
        b = busy[0]
        return {**result, "status": "busy",
                "reason": f"Class {b['reason']} from {b['start']:%H:%M} to {b['end']:%H:%M}",
                "alternatives": free_slots(tt, key, day, after=at)}

    for br in tt.breaks:
        if _overlaps(start, end, _minutes(br["start"]), _minutes(br["end"])):
            return {**result, "status": "break",
                    "reason": f"{br['label'].title()} ({br['start']:%H:%M}-{br['end']:%H:%M})",
                    "alternatives": free_slots(tt, key, day, after=at)}

    if blocks:  # only "maybe" blocks left
        b = blocks[0]
        return {**result, "status": "maybe",
                "reason": f"{b['reason']} slot for the class ({b['start']:%H:%M}-{b['end']:%H:%M}). "
                          "They may be busy, so the faculty member will confirm.",
                "alternatives": free_slots(tt, key, day, after=at)}

    return {**result, "status": "free", "reason": "No class at this time", "alternatives": []}


def free_slots(tt: Timetable, key: str, day: int, after: time | None = None,
               min_minutes: int = MIN_SLOT_MINUTES) -> list[dict]:
    """Free windows on a day: not busy, not a break, not a 'maybe' slot."""
    if day not in tt.days:
        return []
    blocked = [(_minutes(b["start"]), _minutes(b["end"])) for b in tt.schedule[key] if b["day"] == day]
    blocked += [(_minutes(b["start"]), _minutes(b["end"])) for b in tt.breaks]
    blocked.sort()

    windows, cursor = [], _minutes(tt.day_start)
    if after:
        cursor = max(cursor, _minutes(after))
    for b_start, b_end in blocked:
        if b_start > cursor:
            windows.append((cursor, b_start))
        cursor = max(cursor, b_end)
    windows.append((cursor, _minutes(tt.day_end)))

    return [{"start": _time(s).strftime("%H:%M"), "end": _time(e).strftime("%H:%M")}
            for s, e in windows if e - s >= min_minutes]


# ---------------------------------------------------------------- CLI

def parse_time(time_text: str) -> time:
    """'10:00' | '2pm' | '2:30 pm' | '14:30' -> time."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?\s*", time_text, re.I)
    if not m:
        raise ValueError(f"Can't read the time {time_text!r}. Use a format like 10:00 or 2pm.")
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap and ap.lower().startswith("p") and h < 12:
        h += 12
    elif ap and ap.lower().startswith("a") and h == 12:
        h = 0
    elif not ap and h < 7:  # same rule as the timetable: '2:00' means 14:00
        h += 12
    return time(h, mins)


def parse_when(day_text: str, time_text: str) -> tuple[int, time, date | None]:
    """('Monday' | '2026-10-05', '10:00' | '2pm') -> (weekday, time, date)."""
    the_date = None
    day = parse_day(day_text)
    if day is None:
        the_date = date.fromisoformat(day_text)
        day = the_date.weekday()
    return day, parse_time(time_text), the_date


def main(argv: list[str]) -> None:
    if len(argv) < 4:
        print(__doc__)
        return
    path, who, day_text, time_text = argv[:4]
    tt = load_timetable(path)

    found = find_faculty(who, tt)
    names = " or ".join(m["name"] for m in found["matches"])
    if found["status"] == "not_found":
        print(f"No faculty matching {who!r} in this timetable")
        return
    if found["status"] == "did_you_mean":
        print(f"Did you mean {names}?")
        return
    if found["status"] == "ambiguous":
        print(f"More than one match for {who!r}: {names}")
        return

    key = found["matches"][0]["key"]
    day, at, _ = parse_when(day_text, time_text)
    r = check_availability(tt, key, day, at)
    print(f"{r['faculty']} on {r['day']} at {r['time']}: {r['status'].upper()}")
    print(f"  {r['reason']}")
    if r["alternatives"]:
        print("  Free later that day: " + ", ".join(f"{a['start']}-{a['end']}" for a in r["alternatives"]))


if __name__ == "__main__":
    main(sys.argv[1:])
