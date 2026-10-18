"""Small text helpers: days, times, course codes, faculty names, rooms."""
from __future__ import annotations

import re
from datetime import time

DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_day(text: str | None) -> int | None:
    """'Monday', 'MON', 'Thu.' -> weekday number. Long sentences don't count."""
    if not text or len(text) > 20:
        return None
    m = re.match(r"\s*([A-Za-z]+)", text)
    return DAY_NAMES.get(m.group(1).lower()) if m else None


# 8:10-9:00 | 8.10 - 9.00 | 2:00 pm to 2:50 pm
TIME_RANGE_RE = re.compile(
    r"(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?\s*(?:-|–|—|to)\s*(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?",
    re.I,
)

# College hours: a bare "2:00" means 14:00. Hours below this are read as PM.
EARLIEST_CLASS_HOUR = 7


def _to_time(h: int, m: int, meridiem: str | None) -> time:
    if meridiem:
        pm = meridiem.lower().startswith("p")
        if pm and h < 12:
            h += 12
        if not pm and h == 12:
            h = 0
    elif h < EARLIEST_CLASS_HOUR:
        h += 12
    return time(h, m)


def parse_time_range(text: str | None) -> tuple[time, time] | None:
    if not text:
        return None
    m = TIME_RANGE_RE.search(text)
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    # "2:00-2:50 pm" -> the pm applies to both ends
    ap1 = ap1 or ap2
    try:
        start = _to_time(int(h1), int(m1), ap1)
        end = _to_time(int(h2), int(m2), ap2)
    except ValueError:
        return None
    return (start, end) if start < end else None


def is_break(text: str | None) -> bool:
    return bool(text and re.search(r"break|lunch|recess|interval", text, re.I))


def is_lab_text(text: str) -> bool:
    return bool(re.search(r"\blab\b|laboratory|practical", text, re.I))


# ---------------------------------------------------------------- course codes

# Generic fallback for codes like 23ECE341, CS101, 19MAT112A
CODE_RE = re.compile(r"\b(?:\d{2})?[A-Z]{2,5}\s?\d{3,4}[A-Z]?\b")


def normalize_code(code: str) -> str:
    return re.sub(r"\s+", "", code).upper()


def extract_codes(text: str, known: set[str]) -> tuple[list[str], list[str]]:
    """Return (known codes found, unknown code-like tokens found)."""
    compact = normalize_code(text)
    found = [c for c in sorted(known, key=len, reverse=True) if c in compact]
    # remove known codes before looking for unknown ones
    rest = text.upper()
    for c in found:
        rest = rest.replace(c, " ")
    unknown = [normalize_code(c) for c in CODE_RE.findall(rest)]
    # keep original cell order
    found.sort(key=lambda c: compact.find(c))
    return found, unknown


def leftover_text(text: str, codes: list[str]) -> str:
    """Text left in a cell after removing codes, 'Lab' and separators."""
    rest = text.upper()
    for c in codes:
        rest = rest.replace(c, " ")
    rest = re.sub(r"\b(LAB|LABORATORY|PRACTICAL)\b", " ", rest)
    rest = re.sub(r"[/,;&+\-()\s]+", " ", rest)
    return rest.strip()


# ---------------------------------------------------------------- names

TITLE_RE = re.compile(r"^(dr|prof|professor|mr|mrs|ms|miss|shri|smt|sri)\b\.?\s*", re.I)
NAME_SPLIT_RE = re.compile(r"\s*(?:/|,|;|&|\+|\n|\band\b)\s*", re.I)


def split_faculty(text: str | None) -> list[str]:
    if not text:
        return []
    names = []
    for p in NAME_SPLIT_RE.split(text):
        p = re.sub(r"\s+", " ", p).strip(" -")
        # drop a single trailing dot ("C."), but keep "..." so cut-off names stay visible
        if p.endswith(".") and not p.endswith(".."):
            p = p[:-1].rstrip()
        # "Dr." alone or an initial left over from a bad split
        if len(re.sub(r"[^A-Za-z]", "", TITLE_RE.sub("", p))) < 2:
            if names and p:
                names[-1] = f"{names[-1]} {p}".strip()
            continue
        names.append(p)
    return names


def is_truncated(name: str) -> bool:
    return name.rstrip().endswith(("...", "…", ".."))


def clean_name(name: str) -> str:
    name = re.sub(r"(\.{2,}|…)\s*$", "", name).strip()
    return re.sub(r"\s+", " ", name)


def name_key(name: str) -> str:
    """'Dr. M. Vinodhini' -> 'm vinodhini'. Used to match the same person."""
    n = clean_name(name)
    prev = None
    while prev != n:
        prev, n = n, TITLE_RE.sub("", n).strip()
    n = re.sub(r"[^A-Za-z\s]", " ", n).lower()
    return re.sub(r"\s+", " ", n).strip()


# ---------------------------------------------------------------- rooms

ROOM_RE = re.compile(r"\b[A-Z]{1,2}\s?-?\d{3}[A-Z]?\b")


def split_title_rooms(title: str) -> tuple[str, list[str]]:
    """'System on Chip (A404/A405/A408)' -> ('System on Chip', ['A404','A405','A408'])"""
    rooms: list[str] = []

    def take(m: re.Match) -> str:
        found = ROOM_RE.findall(m.group(1).upper())
        if found:
            rooms.extend(r.replace(" ", "").replace("-", "") for r in found)
            return ""
        return m.group(0)

    clean = re.sub(r"\(([^)]*)\)", take, title)
    return re.sub(r"\s+", " ", clean).strip(), rooms


def find_default_room(text: str) -> str | None:
    m = re.search(r"(?:class\s*room|room|venue|hall)\s*(?:no\.?)?\s*[:\-]\s*([A-Za-z0-9\-]+)", text, re.I)
    return m.group(1).upper() if m else None


def find_section_info(text: str) -> dict:
    """Pull program / semester / section out of title rows."""
    info: dict = {"program": None, "semester": None, "section": None}
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*sem", text, re.I)
    if m:
        info["semester"] = int(m.group(1))
    m = re.search(r"(?:^|\n)\s*([A-Z][A-Z&.]{1,9})\s*[-–:]\s*\d{1,2}\s*(?:st|nd|rd|th)?\s*sem", text, re.I)
    if m:
        info["program"] = m.group(1).upper().rstrip(".")
    m = re.search(r"\bsec(?:tion)?\.?\s*[:\-]?\s*([A-Z])\b", text, re.I)
    if m:
        info["section"] = m.group(1).upper()
    return info


def parse_ltpc(text: str | None) -> tuple[str | None, int | None, bool]:
    """'3 0 0 3' -> ('3-0-0-3', 3 credits, is_lab=False)."""
    if not text:
        return None, None, False
    nums = re.findall(r"\d+", text)
    if len(nums) != 4:
        return text.strip() or None, None, False
    l, t, p, c = (int(n) for n in nums)
    return "-".join(nums), c, (l == 0 and p > 0)
