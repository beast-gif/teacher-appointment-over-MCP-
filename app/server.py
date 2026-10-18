"""Step 6: the MCP server.

Tools:
    get_today                   today's date and time in IST
    find_faculty                fuzzy name lookup ("Bhavana ma'am")
    check_faculty_availability  free / busy / maybe at a time  (booking intent)
    get_free_slots              free windows on a day
    get_timetable               class schedule questions        (no booking)

Requires mcp 2.x:  pip install "mcp>=2"

Normally you don't run this yourself: the FastAPI app serves it at /mcp
(uvicorn app.api.main:create_app --factory). To run it on its own, from tt_mcp:
    python -m app.server           # stdio: for MCP Inspector
    python -m app.server --http    # HTTP at http://localhost:8000/mcp

Settings (environment variables):
    TIMETABLE_FILE   default: EAC_7th_sem_timetable.xlsx
    HOST, PORT       default: 127.0.0.1, 8000 (HTTP mode only)
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from datetime import date, datetime, time as Time, timedelta, timezone

from mcp.server.mcpserver import MCPServer  # mcp 2.x (was FastMCP in 1.x)

from .availability import (
    Timetable,
    check_availability,
    find_faculty as _find_faculty,
    free_slots,
    load_timetable,
    parse_time,
)
from .ingestion.text import DAY_LABELS, parse_day

# Fixed offset instead of zoneinfo: Windows Python has no timezone database by default.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

TIMETABLE_FILE = os.getenv("TIMETABLE_FILE", "EAC_7th_sem_timetable.xlsx")

# The timetable is loaded on first use, so FastAPI can hand over the copy it
# already has (use_timetable) instead of reading the Excel file twice.
log = logging.getLogger("mcp_server")

_state: dict = {"tt": None, "bookings": None}


def use_timetable(timetable: Timetable) -> None:
    _state["tt"] = timetable


def use_bookings(lookup: Callable[[str, date], list[tuple[Time, Time]]]) -> None:
    """FastAPI hands over a function returning the (start, end) of every pending,
    accepted or proposed appointment for a faculty member on a date. Without it
    (running this file on its own) answers are timetable-only."""
    _state["bookings"] = lookup


def _held(faculty_id: str, d: date) -> list[tuple[Time, Time]]:
    lookup = _state["bookings"]
    if lookup is None:
        return []
    try:
        return sorted(lookup(faculty_id, d))
    except Exception:
        # Fail safe: answer from the timetable alone. Booking itself still
        # refuses clashes, so a wrong "free" here can't double-book anyone.
        log.exception("appointment lookup failed; answering from the timetable only")
        return []


def _mins(t: Time) -> int:
    return t.hour * 60 + t.minute


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _minus_bookings(windows: list[dict], held: list[tuple[Time, Time]], min_minutes: int) -> list[dict]:
    """Cut booked appointments out of free windows. 15:40-16:20 minus 15:40-15:55 -> 15:55-16:20."""
    out = []
    for w in windows:
        pieces = [(_mins(parse_time(w["start"])), _mins(parse_time(w["end"])))]
        for hs, he in ((_mins(a), _mins(b)) for a, b in held):
            next_pieces = []
            for s, e in pieces:
                if he <= s or hs >= e:
                    next_pieces.append((s, e))
                    continue
                if hs > s:
                    next_pieces.append((s, hs))
                if he < e:
                    next_pieces.append((he, e))
            pieces = next_pieces
        out += [{"start": _hhmm(s), "end": _hhmm(e)} for s, e in pieces if e - s >= min_minutes]
    return out


def _tt() -> Timetable:
    if _state["tt"] is None:
        _state["tt"] = load_timetable(TIMETABLE_FILE)
    return _state["tt"]


mcp = MCPServer(
    "faculty-timetable",
    instructions=(
        "Class timetable and faculty availability. "
        "Use get_timetable for questions about classes, rooms or what someone teaches. "
        "Use check_faculty_availability only when the student asks whether a faculty "
        "member is free or available. Times are IST. These tools never book anything."
    ),
)


# ---------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(IST)


def _resolve_day(text: str) -> tuple[int, date]:
    """'today' | 'tomorrow' | 'Monday' | '2026-10-05' -> (weekday, date).

    A weekday name means the next time that day comes round (today counts).
    """
    t = text.strip().lower()
    today = _now().date()
    if t == "today":
        d = today
    elif t == "tomorrow":
        d = today + timedelta(days=1)
    elif (wd := parse_day(t)) is not None:
        d = today + timedelta(days=(wd - today.weekday()) % 7)
    else:
        try:
            d = date.fromisoformat(t)
        except ValueError:
            raise ValueError(f"Can't read the day {text!r}. Use today, tomorrow, a weekday, or YYYY-MM-DD.")
    return d.weekday(), d


def _resolve_person(name: str) -> tuple[str | None, dict | None]:
    """(faculty key, None) when there's one clear match, else (None, reply for the LLM)."""
    found = _find_faculty(name, _tt())
    if found["status"] == "found":
        return found["matches"][0]["key"], None
    return None, {
        "status": {"not_found": "faculty_not_found"}.get(found["status"], found["status"]),
        "query": name,
        "matches": [m["name"] for m in found["matches"]],
        "hint": {
            "not_found": "No faculty with that name teaches this class. Ask the student to check the name.",
            "did_you_mean": "Ask the student to confirm the name before checking again.",
            "ambiguous": "Ask the student which of these they mean.",
        }[found["status"]],
    }


def _course_info(code: str) -> dict:
    c = _tt().courses[code]
    return {
        "code": code,
        "title": c["title"],
        "faculty": [_tt().people[k]["name"] for k in _tt().people if code in _tt().people[k]["courses"]],
        "rooms": c["rooms"] or [_tt().section.get("default_room")],
    }


# ---------------------------------------------------------------- tools

@mcp.tool()
def get_today() -> dict:
    """Today's date, weekday and current time in IST.

    Use this to work out dates when the student says things like
    'tomorrow' or 'next Monday'.
    """
    now = _now()
    return {"date": now.date().isoformat(), "day": DAY_LABELS[now.weekday()],
            "time": now.strftime("%H:%M"), "timezone": "IST"}


@mcp.tool()
def find_faculty(name: str) -> dict:
    """Look up a faculty member by (part of) their name.

    Handles informal names like "Bhavana ma'am" or "Ganapathi sir" and small typos.
    Returns status found / did_you_mean / ambiguous / not_found, with matching names
    and the courses they teach for this class.
    """
    found = _find_faculty(name, _tt())
    return {
        "status": found["status"],
        "matches": [
            {"faculty_id": m["key"], "name": m["name"], "courses": [_course_info(c)["title"] + f" ({c})" for c in _tt().people[m["key"]]["courses"]]}
            for m in found["matches"]
        ],
    }


@mcp.tool()
def check_faculty_availability(faculty_name: str, day: str, time: str, duration_minutes: int = 15) -> dict:
    """Check whether a faculty member is free at a given time.

    Use ONLY when the student asks if a faculty member is free or available.
    Not for general timetable questions (use get_timetable for those).

    Args:
        faculty_name: name as the student wrote it, e.g. "Bhavana ma'am".
        day: "today", "tomorrow", a weekday like "Monday", or a date "YYYY-MM-DD".
        time: e.g. "10:00", "2pm", "14:30". A bare "2:00" means 2 pm.
        duration_minutes: length of the meeting, default 15.

    Returns status free / busy / maybe / break / outside_hours, a reason, free
    alternatives later that day, and "bookable" (true for free or maybe).
    Booked appointments count as busy, just like classes.
    """
    key, problem = _resolve_person(faculty_name)
    if problem:
        return problem
    weekday, d = _resolve_day(day)
    at = parse_time(time)
    result = check_availability(_tt(), key, weekday, at, duration_minutes)

    held = _held(key, d)
    if result["status"] in ("free", "maybe"):
        start, end = _mins(at), _mins(at) + duration_minutes
        clash = next(((a, b) for a, b in held if _mins(a) < end and start < _mins(b)), None)
        if clash:
            result = {**result, "status": "busy",
                      "reason": f"Already has an appointment from {clash[0]:%H:%M} to {clash[1]:%H:%M}",
                      "alternatives": free_slots(_tt(), key, weekday, after=at)}
    result["alternatives"] = _minus_bookings(result.get("alternatives", []), held, duration_minutes)

    return {**result, "faculty_id": key, "date": d.isoformat(),
            "bookable": result["status"] in ("free", "maybe")}


@mcp.tool()
def get_free_slots(faculty_name: str, day: str) -> dict:
    """List the times a faculty member has no class on a given day.

    Args:
        faculty_name: name as the student wrote it.
        day: "today", "tomorrow", a weekday like "Monday", or a date "YYYY-MM-DD".

    Excludes classes, booked appointments, tea and lunch breaks, and Evaluation
    or Counselling slots.
    """
    key, problem = _resolve_person(faculty_name)
    if problem:
        return problem
    weekday, d = _resolve_day(day)
    return {
        "faculty": _tt().people[key]["name"],
        "faculty_id": key,
        "day": DAY_LABELS[weekday],
        "date": d.isoformat(),
        "free_slots": _minus_bookings(free_slots(_tt(), key, weekday), _held(key, d), 15),
    }


@mcp.tool()
def get_timetable(day: str | None = None, faculty_name: str | None = None) -> dict:
    """Class timetable: which class is when, in which room, taught by whom.

    Use for timetable questions like "what's on Monday", "when is the DSP class",
    or "what does Dr. X teach on Thursday". Never offer to book from this.

    Args:
        day: optional. "today", "tomorrow", a weekday, or "YYYY-MM-DD". Omit for the whole week.
        faculty_name: optional. Only show classes this faculty member teaches.
    """
    weekday = None
    if day:
        weekday, _ = _resolve_day(day)

    codes = None
    faculty = None
    if faculty_name:
        key, problem = _resolve_person(faculty_name)
        if problem:
            return problem
        faculty = _tt().people[key]["name"]
        codes = set(_tt().people[key]["courses"])

    entries = []
    for s in _tt().sessions:
        if weekday is not None and s["day"] != weekday:
            continue
        if codes is not None and not codes & set(s["codes"]):
            continue
        entry = {
            "day": DAY_LABELS[s["day"]],
            "start": s["start"].strftime("%H:%M"),
            "end": s["end"].strftime("%H:%M"),
            "kind": s["kind"],
        }
        if s["codes"]:
            shown = [c for c in s["codes"] if codes is None or c in codes]
            entry["courses"] = [_course_info(c) for c in shown]
            if len(s["codes"]) > 1:
                entry["note"] = "Elective slot: each student attends their own elective."
        else:
            entry["activity"] = s["label"]
        entries.append(entry)

    if weekday is not None and weekday not in _tt().days:
        return {"section": _tt().section, "day": DAY_LABELS[weekday], "entries": [],
                "note": "No classes on this day."}
    return {"section": _tt().section, "faculty": faculty,
            "day": DAY_LABELS[weekday] if weekday is not None else "whole week",
            "entries": entries}


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run(
            transport="streamable-http",
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
        )
    else:
        mcp.run(transport="stdio")