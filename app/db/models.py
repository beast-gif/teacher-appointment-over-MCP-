"""Database models.

Works on SQLite for development and Postgres in production (set DATABASE_URL).

Timetable data is versioned in batches: an uploaded file becomes a batch in
`pending_review`, an admin approves it, and only approved batches are used to
answer availability questions.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- people

class Faculty(Base):
    __tablename__ = "faculty"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Lower-case name without titles ("dr", "ms" ...), used for matching.
    name_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    department: Mapped[str | None] = mapped_column(String(100))
    # Other spellings seen in timetables, e.g. a truncated "Dr. Sreja Koshy...".
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------- timetable

class BatchStatus(str, enum.Enum):
    pending_review = "pending_review"
    approved = "approved"
    superseded = "superseded"
    rejected = "rejected"


class TimetableBatch(Base):
    """One uploaded timetable file."""

    __tablename__ = "timetable_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file: Mapped[str] = mapped_column(String(300))
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus), default=BatchStatus.pending_review)
    report: Mapped[dict] = mapped_column(JSON, default=dict)  # parse issues, counts
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[TimetableSession]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    section_courses: Mapped[list[SectionCourse]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class Section(Base):
    """A class group, e.g. EAC semester 7."""

    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)  # "EAC-S7" or "CSE-S5-A"
    program: Mapped[str | None] = mapped_column(String(40))
    semester: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(10))
    default_room: Mapped[str | None] = mapped_column(String(40))
    breaks: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{"label","start","end"}]


class Course(Base):
    __tablename__ = "courses"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    ltpc: Mapped[str | None] = mapped_column(String(20))
    credits: Mapped[int | None] = mapped_column(Integer)
    is_lab: Mapped[bool] = mapped_column(Boolean, default=False)


class SectionCourse(Base):
    """Who teaches a course for one section in one batch.

    The same course can have different faculty in different sections,
    so the faculty link lives here, not on Course.
    """

    __tablename__ = "section_courses"
    __table_args__ = (UniqueConstraint("batch_id", "section_id", "course_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("timetable_batches.id", ondelete="CASCADE"))
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id"))
    course_code: Mapped[str] = mapped_column(ForeignKey("courses.code"))
    rooms: Mapped[list[str]] = mapped_column(JSON, default=list)

    batch: Mapped[TimetableBatch] = relationship(back_populates="section_courses")
    faculty: Mapped[list[Faculty]] = relationship(secondary="section_course_faculty")


class SectionCourseFaculty(Base):
    __tablename__ = "section_course_faculty"

    section_course_id: Mapped[int] = mapped_column(
        ForeignKey("section_courses.id", ondelete="CASCADE"), primary_key=True
    )
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"), primary_key=True)


class SessionKind(str, enum.Enum):
    lecture = "lecture"
    lab = "lab"
    activity = "activity"  # "Evaluation", "Counselling" ... no course attached


class TimetableSession(Base):
    """One scheduled block. An elective slot with 3 courses becomes 3 rows
    sharing the same slot_group."""

    __tablename__ = "timetable_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("timetable_batches.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("sections.id"), index=True)
    day: Mapped[int] = mapped_column(Integer, index=True)  # 0 = Monday
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)
    kind: Mapped[SessionKind] = mapped_column(Enum(SessionKind))
    course_code: Mapped[str | None] = mapped_column(ForeignKey("courses.code"))
    label: Mapped[str | None] = mapped_column(String(200))
    room: Mapped[str | None] = mapped_column(String(80))
    slot_group: Mapped[str] = mapped_column(String(80))
    source_cell: Mapped[str] = mapped_column(String(80))

    batch: Mapped[TimetableBatch] = relationship(back_populates="sessions")
    section: Mapped[Section] = relationship()
    course: Mapped[Course | None] = relationship()


# ---------------------------------------------------------------- appointments
#
# Times on appointments are IST wall-clock (date + time), because that's how
# people talk about them. Audit timestamps (created_at ...) are naive UTC.

def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AppointmentStatus(str, enum.Enum):
    draft = "draft"              # form filled, student hasn't confirmed the preview
    pending = "pending"          # email sent, waiting for faculty
    rescheduled = "rescheduled"  # faculty proposed another time, waiting for student
    accepted = "accepted"
    declined = "declined"
    cancelled = "cancelled"
    expired = "expired"


class Appointment(Base):
    __tablename__ = "appointments"

    # Random, unguessable: the id is all the chat needs to poll or cancel.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[AppointmentStatus] = mapped_column(Enum(AppointmentStatus), index=True)

    # Student details, as entered on the form
    student_name: Mapped[str] = mapped_column(String(120))
    roll_no: Mapped[str] = mapped_column(String(40), index=True)
    branch: Mapped[str] = mapped_column(String(100))
    semester: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(20))
    student_email: Mapped[str] = mapped_column(String(254))

    # Faculty: id is the timetable key ("bhavana v"); name and email come from
    # the server's own data, never from the browser.
    faculty_id: Mapped[str] = mapped_column(String(120), index=True)
    faculty_name: Mapped[str] = mapped_column(String(160))
    faculty_email: Mapped[str | None] = mapped_column(String(254))

    # The requested (or, once agreed, the confirmed) slot
    date: Mapped[date] = mapped_column(Date, index=True)
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)
    duration_min: Mapped[int] = mapped_column(Integer)

    purpose: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)            # editable on the preview
    faculty_note: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)   # required when the faculty already knew

    # Set while status is "rescheduled"
    proposed_date: Mapped[date | None] = mapped_column(Date)
    proposed_start: Mapped[time | None] = mapped_column(Time)
    proposed_end: Mapped[time | None] = mapped_column(Time)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    student_responded_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # for pending / rescheduled


class AppointmentToken(Base):
    """Single-use link secret. Only the SHA-256 hash is stored."""

    __tablename__ = "appointment_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    appointment_id: Mapped[str] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))   # "faculty" or "student"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)