"""Step 4: link timetable blocks to faculty and build each person's week.

Nothing is saved yet. Run:
    python -m app.ingestion.faculty EAC_7th_sem_timetable.xlsx
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

from .extract import extract_sessions
from .grid import Grid
from .reader import find_days, find_legend, find_time_header, read_lab_slots, read_legend, read_slots
from .text import DAY_LABELS, clean_name, is_truncated, name_key

# What an activity block means for the faculty who teach that class.
#   "maybe"  -> they might be busy; the bot warns but still allows a request
#   "ignore" -> doesn't affect availability
ACTIVITY_RULES = {
    "evaluation": "maybe",
    "counselling": "maybe",
    "counseling": "maybe",
}


def activity_status(label: str) -> str:
    low = label.lower()
    return next((status for word, status in ACTIVITY_RULES.items() if word in low), "ignore")


def load_name_fixes(path: str = "name_fixes.json") -> dict[str, str]:
    """{name as written in the timetable: full correct name}, matched by name_key."""
    p = Path(path)
    if not p.exists():
        return {}
    return {name_key(k): v for k, v in json.loads(p.read_text(encoding="utf-8")).items()}


def build_faculty(courses: list[dict], fixes: dict[str, str]) -> tuple[dict[str, dict], list[str]]:
    """One entry per person, even if they teach several courses.

    Returns ({key: {"name", "courses"}}, warnings).
    """
    people: dict[str, dict] = {}
    warnings = []
    for course in courses:
        for raw in course["faculty"]:
            fixed = fixes.get(name_key(raw))
            if fixed:
                name = fixed
            else:
                name = clean_name(raw)
                if is_truncated(raw):
                    warnings.append(f"{course['code']}: '{raw}' is cut off. Add the full name to name_fixes.json")
            key = name_key(name)
            person = people.setdefault(key, {"name": name, "courses": []})
            if course["code"] not in person["courses"]:
                person["courses"].append(course["code"])
    return people, warnings


def faculty_schedule(sessions: list[dict], people: dict[str, dict]) -> dict[str, list[dict]]:
    """{faculty key: [blocks]}, each block marked "busy" or "maybe"."""
    by_course: dict[str, list[str]] = {}
    for key, p in people.items():
        for code in p["courses"]:
            by_course.setdefault(code, []).append(key)
    everyone = list(people)  # all faculty who teach this class

    schedule: dict[str, list[dict]] = {k: [] for k in people}
    for s in sessions:
        if s["codes"]:
            for code in s["codes"]:
                for key in by_course.get(code, []):
                    schedule[key].append({**s, "status": "busy", "reason": code})
        elif activity_status(s["label"]) == "maybe":
            for key in everyone:
                schedule[key].append({**s, "status": "maybe", "reason": s["label"]})

    for blocks in schedule.values():
        blocks.sort(key=lambda b: (b["day"], b["start"]))
    return schedule


def main(path: str) -> None:
    grid = Grid.from_worksheet(openpyxl.load_workbook(path).worksheets[0])
    day_col, day_rows = find_days(grid)
    header_row = find_time_header(grid, day_col, min(day_rows))
    slots, _ = read_slots(grid, header_row, day_col)
    labs = read_lab_slots(grid, header_row, min(day_rows), day_col)
    legend = find_legend(grid)
    courses = read_legend(grid, legend) if legend else []
    sessions, warnings = extract_sessions(grid, day_rows, slots, labs, courses)

    people, name_warnings = build_faculty(courses, load_name_fixes())
    schedule = faculty_schedule(sessions, people)

    fmt = lambda t: t.strftime("%H:%M")
    for key, person in sorted(people.items(), key=lambda kv: kv[1]["name"]):
        blocks = schedule[key]
        busy = sum(1 for b in blocks if b["status"] == "busy")
        print(f"\n{person['name']}  ({', '.join(person['courses'])})  {busy} busy blocks")
        for b in blocks:
            if b["status"] == "busy":
                print(f"    {DAY_LABELS[b['day']][:3]} {fmt(b['start'])}-{fmt(b['end'])}  busy   {b['reason']}")

    print(f"\n{len(people)} faculty found")
    maybe = sorted({(s['day'], s['start'], s['end'], s['label']) for s in sessions
                    if not s["codes"] and activity_status(s["label"]) == "maybe"})
    print("\n'Maybe busy' slots (apply to every faculty member above):")
    for day, start, end, label in maybe:
        print(f"    {DAY_LABELS[day][:3]} {fmt(start)}-{fmt(end)}  {label}")

    if warnings or name_warnings:
        print("\nWarnings:")
        for w in warnings + name_warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "EAC_7th_sem_timetable.xlsx")
