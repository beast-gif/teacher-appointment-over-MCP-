"""One place for "now", so tests can freeze it.

Always call these as clock.now_ist(), never import the functions directly.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    """Current IST wall-clock time, without tzinfo (matches how slots are stored)."""
    return datetime.now(IST).replace(tzinfo=None)


def utcnow() -> datetime:
    """Current UTC time, without tzinfo (matches the audit columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
