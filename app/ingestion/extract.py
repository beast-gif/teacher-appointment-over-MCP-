"""Step 3: turn the grid into a list of classes per day.

Nothing is saved yet. Run:
    python -m app.ingestion.extract EAC_7th_sem_timetable.xlsx
"""
from __future__ import annotations

import sys

import openpyxl

from .grid import Grid
from .reader import find_days, find_legend, find_time_header, read_lab_slots, read_legend, read_slots
from .text import DAY_LABELS, extract_codes, is_break, is_lab_text, leftover_text


def extract_sessions(grid: Grid, day_rows: dict[int, int], slots: list[dict],
                     labs: list[dict], courses: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (sessions, warnings).

    One session per timetable box. An elective box like
    '23ECE341/23ECE451/23ECE436' is one session with three course codes.
    """
    slot_by_col = {s["col"]: s for s in slots}
    course_by_code = {c["code"]: c for c in courses}
    sessions, warnings = [], []

    for row, day in sorted(day_rows.items()):
        done = set()
        for col in slot_by_col:
            text = grid.value(row, col)
            if not text or is_break(text):
                continue

            # A merged box covers several periods: handle it once per day.
            span = grid.span(row, col)
            if span in done:
                continue
            done.add(span)

            cols = [c for c in range(span[1], span[3] + 1) if c in slot_by_col]
            start, end = slot_by_col[cols[0]]["start"], slot_by_col[cols[-1]]["end"]
            cell = grid.span_coord(span)

            codes, unknown = extract_codes(text, set(course_by_code))
            for u in unknown:
                warnings.append(f"{cell}: code {u} is not in the legend")

            if codes:
                is_lab = is_lab_text(text) or all(course_by_code[c]["is_lab"] for c in codes)
                kind = "lab" if is_lab else "lecture"
                # Labs run on the lab clock when the box covers a whole lab slot.
                if is_lab:
                    for lab in labs:
                        if lab["min_col"] == span[1] and lab["max_col"] == span[3]:
                            start, end = lab["start"], lab["end"]
                            break
                extra = leftover_text(text, codes)
                if extra:
                    warnings.append(f"{cell}: ignored extra text {extra!r}")
                label = None
            else:
                # 'Evaluation', 'Counselling', 'DEPARTURE OF BUSES' ...
                kind, label = "activity", " ".join(text.split())

            sessions.append({
                "day": day, "start": start, "end": end, "kind": kind,
                "codes": codes, "label": label, "cell": cell,
            })

    return sessions, warnings


def main(path: str) -> None:
    grid = Grid.from_worksheet(openpyxl.load_workbook(path).worksheets[0])
    day_col, day_rows = find_days(grid)
    header_row = find_time_header(grid, day_col, min(day_rows))
    slots, _ = read_slots(grid, header_row, day_col)
    labs = read_lab_slots(grid, header_row, min(day_rows), day_col)
    legend = find_legend(grid)
    courses = read_legend(grid, legend) if legend else []

    sessions, warnings = extract_sessions(grid, day_rows, slots, labs, courses)

    fmt = lambda t: t.strftime("%H:%M")
    current = None
    for s in sessions:
        if s["day"] != current:
            current = s["day"]
            print(f"\n{DAY_LABELS[current]}")
        what = " / ".join(s["codes"]) if s["codes"] else s["label"]
        print(f"  {fmt(s['start'])}-{fmt(s['end'])}  {s['kind']:<8}  {what:<45}  [{s['cell']}]")

    print(f"\n{len(sessions)} sessions found")
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "EAC_7th_sem_timetable.xlsx")
