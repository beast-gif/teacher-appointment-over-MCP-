"""Shared state, filled in once by create_app()."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session, sessionmaker

from ..availability import Timetable
from ..directory import FacultyDirectory
from .config import Settings


class Context:
    settings: Settings
    tt: Timetable
    sessions: sessionmaker[Session]
    directory: FacultyDirectory
    mailer: object  # FileMailer | SmtpMailer | GraphMailer


ctx = Context()


def get_db() -> Iterator[Session]:
    db = ctx.sessions()
    try:
        yield db
    finally:
        db.close()
