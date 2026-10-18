"""Faculty email addresses, from faculty_directory.csv.

Columns: faculty_id, name, courses, email. Only faculty_id and email are used.
A missing or blank email means "we don't know": the backend never guesses one.

Make a fresh template from the timetable (won't overwrite an existing file):
    python -m app.directory
"""
from __future__ import annotations

import csv
import logging
import re
import sys
from pathlib import Path

from .ingestion.text import name_key

log = logging.getLogger("directory")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class FacultyDirectory:
    def __init__(self, emails: dict[str, str] | None = None):
        self._emails = emails or {}

    @classmethod
    def load(cls, path: str) -> FacultyDirectory:
        p = Path(path)
        if not p.exists():
            log.warning("no faculty directory at %s: no faculty emails known", p)
            return cls()
        emails = {}
        with p.open(encoding="utf-8-sig", newline="") as f:
            for i, row in enumerate(csv.DictReader(f), start=2):
                email = (row.get("email") or "").strip()
                if not email:
                    continue
                if not EMAIL_RE.match(email):
                    log.warning("%s line %d: %r isn't an email address, skipped", p, i, email)
                    continue
                key = (row.get("faculty_id") or "").strip() or name_key(row.get("name") or "")
                if key:
                    emails[key] = email.lower()
        log.info("faculty directory: %d emails", len(emails))
        return cls(emails)

    def email_for(self, faculty_id: str) -> str | None:
        return self._emails.get(faculty_id)

    def __len__(self) -> int:
        return len(self._emails)


def write_template(timetable_file: str, out: str = "faculty_directory.csv") -> None:
    from .availability import load_timetable

    if Path(out).exists():
        print(f"{out} already exists, not overwriting it.")
        return
    tt = load_timetable(timetable_file)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["faculty_id", "name", "courses", "email"])
        for key, p in sorted(tt.people.items(), key=lambda kv: kv[1]["name"]):
            w.writerow([key, p["name"], " ".join(p["courses"]), ""])
    print(f"Wrote {out} with {len(tt.people)} faculty. Fill in the email column.")


if __name__ == "__main__":
    write_template(sys.argv[1] if len(sys.argv) > 1 else "EAC_7th_sem_timetable.xlsx")
