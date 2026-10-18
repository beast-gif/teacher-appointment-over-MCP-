"""Step 8: appointment booking API.

Routes match the frontend prompt exactly:

    POST /appointments                          form  -> draft + email preview (sends nothing)
    POST /appointments/{id}/confirm             draft -> pending (the ONLY place the request email is sent)
    POST /appointments/{id}/cancel              draft / pending / rescheduled -> cancelled
    GET  /appointments/{id}/status              booking_status card for the chat to poll
    GET  /appointments/{id}/respond?token=      faculty page data (changes nothing)
    POST /appointments/{id}/respond             faculty: accept / decline / propose
    GET  /appointments/{id}/student-response?token=   reschedule page data (changes nothing)
    POST /appointments/{id}/student-response    student: accept / decline the new time (email link)
    POST /appointments/lookup                   "My appointments": status of the ids the browser saved
    POST /appointments/{id}/proposal            student: accept / decline the new time (from the list)

No LLM is involved anywhere here. The checks (faculty exists, slot is free,
no clash) call the same timetable code the MCP server uses, directly.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..availability import check_availability, free_slots, parse_time
from ..db.models import Appointment, AppointmentStatus as S, AppointmentToken
from . import clock, emails
from .context import ctx, get_db
from .mailer import Mail, MailError

log = logging.getLogger("appointments")
router = APIRouter(tags=["appointments"])

ROLL_RE = re.compile(r"^[A-Z]{2}\.[A-Z]{2}\.[A-Z0-9]+$")
WAITING = (S.pending, S.rescheduled)          # waiting on someone's reply
HOLDS_SLOT = (S.pending, S.accepted)          # blocks the faculty member's time


# ================================================================ payloads

class StudentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    roll_no: str = Field(min_length=1, max_length=40)
    branch: str = Field(min_length=1, max_length=100)
    semester: int = Field(ge=1, le=8)
    section: str | None = Field(default=None, max_length=20)
    email: str = Field(min_length=3, max_length=254)

    @field_validator("name", "branch")
    @classmethod
    def _tidy(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("This field is required.")
        return v

    @field_validator("section")
    @classmethod
    def _section(cls, v: str | None) -> str | None:
        v = (v or "").strip().upper()
        return v or None

    @field_validator("roll_no")
    @classmethod
    def _roll(cls, v: str) -> str:
        v = v.strip().upper()
        if not ROLL_RE.match(v):
            raise ValueError("That roll number doesn't look right, e.g. BL.EN.U4EAC23051.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        domain = ctx.settings.allowed_email_domain
        if not re.fullmatch(r"[^@\s]+@" + re.escape(domain), v):
            raise ValueError(f"Use your college email, ending in @{domain}.")
        return v


class CreateIn(BaseModel):
    faculty_id: str = Field(min_length=1, max_length=120)
    date: date
    time: str = Field(min_length=1, max_length=10)
    duration_min: Literal[15, 30, 45] = 15
    student: StudentIn
    purpose: str = Field(min_length=1, max_length=300)

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Tell the faculty member what it's about.")
        return v


class ConfirmIn(BaseModel):
    message: str | None = Field(default=None, max_length=1000)


class FacultyRespondIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    action: Literal["accept", "decline", "propose"]
    note: str | None = Field(default=None, max_length=500)
    proposed_slot: str | None = None


class StudentRespondIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    action: Literal["accept", "decline"]


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


class LookupIn(BaseModel):
    ids: list[str] = Field(max_length=50)


class ProposalAnswerIn(BaseModel):
    action: Literal["accept", "decline"]


MIN_REASON = 5


# ================================================================ helpers

def iso_slot(d: date | None, t: time | None) -> str | None:
    """2026-10-05, 11:00 -> '2026-10-05T11:00:00+05:30'"""
    return f"{d.isoformat()}T{t:%H:%M}:00+05:30" if d and t else None


def add_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def overlaps(a0: time, a1: time, b0: time, b1: time) -> bool:
    return a0 < b1 and b0 < a1


def status_view(a: Appointment) -> dict:
    """The booking_status message the chat renders."""
    return {
        "type": "booking_status",
        "appointment_id": a.id,
        "status": a.status.value,
        "faculty": a.faculty_name,
        "slot": iso_slot(a.date, a.start),
        "end": f"{a.end:%H:%M}",
        "duration_min": a.duration_min,
        "proposed_slot": iso_slot(a.proposed_date, a.proposed_start),
        "faculty_note": a.faculty_note,
        "cancel_reason": a.cancel_reason,
    }


def can_cancel(a: Appointment) -> tuple[bool, bool]:
    """(cancel allowed, reason required). Drafts were never sent, so no reason needed."""
    if a.status == S.draft:
        return True, False
    if a.status in WAITING:
        return True, True
    if a.status == S.accepted and datetime.combine(a.date, a.start) > clock.now_ist():
        return True, True
    return False, False


def get_appointment(db: Session, appointment_id: str) -> Appointment:
    a = db.get(Appointment, appointment_id)
    if a is None:
        raise HTTPException(404, "No such appointment.")
    return a


def held_slots(db: Session, faculty_id: str, d: date, exclude_id: str | None = None):
    """(start, end, appointment) for everything already holding this faculty member's time."""
    rows = db.scalars(
        select(Appointment).where(
            Appointment.faculty_id == faculty_id,
            Appointment.status.in_([*HOLDS_SLOT, S.rescheduled]),
        )
    ).all()
    out = []
    for a in rows:
        if a.id == exclude_id:
            continue
        if a.status in HOLDS_SLOT and a.date == d:
            out.append((a.start, a.end, a))
        elif a.status == S.rescheduled and a.proposed_date == d:   # the proposed time is on hold
            out.append((a.proposed_start, a.proposed_end, a))
    return out


def check_slot(db: Session, faculty_id: str, d: date, start: time, duration: int,
               exclude_id: str | None = None) -> time:
    """Raise 409 with a readable reason unless the slot can be booked. Returns the end time."""
    tt = ctx.tt
    name = tt.people[faculty_id]["name"]
    now = clock.now_ist()
    if datetime.combine(d, start) <= now:
        raise HTTPException(409, "That time has already passed. Pick a later time.")
    if (d - now.date()).days > ctx.settings.max_days_ahead:
        raise HTTPException(409, f"Pick a date within the next {ctx.settings.max_days_ahead} days.")

    result = check_availability(tt, faculty_id, d.weekday(), start, duration)
    if result["status"] not in ("free", "maybe"):
        raise HTTPException(409, f"{name} isn't available then. {result['reason']}.")

    end = add_minutes(start, duration)
    for s, e, _ in held_slots(db, faculty_id, d, exclude_id):
        if overlaps(start, end, s, e):
            raise HTTPException(
                409, f"{name} already has an appointment from {emails.pretty_time(s)} to "
                     f"{emails.pretty_time(e)} that day. Pick another time.")
    return end


def refresh_expiry(db: Session, a: Appointment) -> None:
    """Pending or rescheduled requests expire when the 24 hours run out, or the time passes."""
    if a.status not in WAITING:
        return
    slot_d, slot_t = (a.date, a.start) if a.status == S.pending else (a.proposed_date, a.proposed_start)
    lapsed = a.expires_at is not None and a.expires_at <= clock.utcnow()
    passed = datetime.combine(slot_d, slot_t) <= clock.now_ist()
    if lapsed or passed:
        a.status = S.expired
        _burn_tokens(db, a.id)
        db.commit()


# ---------------------------------------------------------------- tokens

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint_token(db: Session, appointment_id: str, kind: str) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(AppointmentToken(
        token_hash=_hash(raw), appointment_id=appointment_id, kind=kind,
        created_at=clock.utcnow(),
        expires_at=clock.utcnow() + timedelta(hours=ctx.settings.token_ttl_hours),
    ))
    return raw


def _burn_tokens(db: Session, appointment_id: str) -> None:
    db.execute(update(AppointmentToken)
               .where(AppointmentToken.appointment_id == appointment_id, AppointmentToken.used_at.is_(None))
               .values(used_at=clock.utcnow()))


def open_link(db: Session, appointment_id: str, raw: str, kind: str,
              expected: S) -> tuple[Appointment, AppointmentToken]:
    """Check a faculty or student link without using it up. Every failure is 410."""
    tok = db.get(AppointmentToken, _hash(raw))
    a = db.get(Appointment, appointment_id)
    if tok is None or a is None or tok.appointment_id != appointment_id or tok.kind != kind:
        raise HTTPException(410, "This link isn't valid.")
    if a.status == S.cancelled:
        raise HTTPException(410, "The student cancelled this request.")
    if tok.used_at is not None and a.status != S.expired:
        raise HTTPException(410, "This request was already answered.")
    refresh_expiry(db, a)
    if a.status == S.expired or tok.expires_at <= clock.utcnow():
        raise HTTPException(410, "This link has expired.")
    if a.status != expected:
        raise HTTPException(410, "This request was already answered.")
    return a, tok


def spend_token(db: Session, tok: AppointmentToken) -> None:
    """Mark the link used. Atomic: a double click can't use it twice."""
    done = db.execute(update(AppointmentToken)
                      .where(AppointmentToken.token_hash == tok.token_hash, AppointmentToken.used_at.is_(None))
                      .values(used_at=clock.utcnow())).rowcount
    if done != 1:
        db.rollback()
        raise HTTPException(410, "This request was already answered.")


def claim(db: Session, a: Appointment, expected: S, **values) -> None:
    """Move to a new status only if nobody else changed it first."""
    done = db.execute(update(Appointment)
                      .where(Appointment.id == a.id, Appointment.status == expected)
                      .values(**values)).rowcount
    if done != 1:
        db.rollback()
        raise HTTPException(410, "This request was already answered.")


def notify(to: str | None, subject: str, html: str, text: str, reply_to: str | None = None) -> bool:
    """Send a follow-up email. Failures are logged, not raised: the decision already stands."""
    try:
        ctx.mailer.send(Mail(to=to or "unknown-faculty@invalid", subject=subject,
                             html=html, text=text, reply_to=reply_to))
        return True
    except MailError:
        log.exception("follow-up email to %s failed", to)
        return False


def parse_slot(text: str) -> tuple[date, time]:
    """'2026-10-06T14:00', '2026-10-06T14:00:00+05:30' or '...Z' -> IST date and time."""
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "Couldn't read that date and time.")
    if dt.tzinfo is not None:
        dt = dt.astimezone(clock.IST).replace(tzinfo=None)
    return dt.date(), dt.time().replace(second=0, microsecond=0)


def suggested_slots(db: Session, a: Appointment, limit: int = 6) -> list[dict]:
    """Free windows for the faculty member on the requested day and the next school days."""
    tt, now = ctx.tt, clock.now_ist()
    out, d = [], max(a.date, now.date())
    for _ in range(10):
        if d.weekday() in tt.days:
            after = now.time() if d == now.date() else None
            for w in free_slots(tt, a.faculty_id, d.weekday(), after=after, min_minutes=a.duration_min):
                start, end = parse_time(w["start"]), parse_time(w["end"])
                if d == a.date and start <= a.start < end:
                    continue   # that's the slot they're answering about
                if any(overlaps(start, end, s, e) for s, e, _ in held_slots(db, a.faculty_id, d, a.id)):
                    continue
                out.append({"date": d.isoformat(), "start": w["start"], "end": w["end"]})
                if len(out) >= limit:
                    return out
        d += timedelta(days=1)
    return out


# ================================================================ student: create, confirm, cancel, status

@router.post("/appointments")
def create_appointment(body: CreateIn, db: Session = Depends(get_db)) -> dict:
    """Save a draft and return the email preview. Sends nothing."""
    tt = ctx.tt
    if body.faculty_id not in tt.people:
        raise HTTPException(404, "We couldn't find that faculty member. Check their availability again.")
    try:
        start = parse_time(body.time)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    end = check_slot(db, body.faculty_id, body.date, start, body.duration_min)

    waiting = db.scalar(select(func.count()).select_from(Appointment).where(
        Appointment.roll_no == body.student.roll_no, Appointment.status.in_(WAITING)))
    if waiting >= ctx.settings.max_waiting_per_student:
        raise HTTPException(429, f"You already have {waiting} requests waiting for a reply. "
                                 "Wait for those, or cancel one, before sending another.")

    faculty_name = tt.people[body.faculty_id]["name"]
    faculty_email = ctx.directory.email_for(body.faculty_id)
    if faculty_email is None and ctx.mailer.real:
        raise HTTPException(409, f"Booking isn't set up for {faculty_name} yet: we don't have their email.")

    s = body.student
    a = Appointment(
        id=uuid.uuid4().hex, status=S.draft,
        student_name=s.name, roll_no=s.roll_no, branch=s.branch, semester=s.semester,
        section=s.section, student_email=s.email,
        faculty_id=body.faculty_id, faculty_name=faculty_name, faculty_email=faculty_email,
        date=body.date, start=start, end=end, duration_min=body.duration_min,
        purpose=body.purpose, message=emails.draft_message(s.name, s.roll_no, body.purpose),
        created_at=clock.utcnow(),
    )
    db.add(a)
    db.commit()

    subject, html, _ = emails.faculty_request(a, links=None)
    return {
        "appointment_id": a.id,
        "status": a.status.value,
        "preview": {
            "subject": subject,
            "to": f"{faculty_name} <{faculty_email}>" if faculty_email else f"{faculty_name} (no email on file yet)",
            "body_html": html,
            "message": a.message,
        },
    }


@router.post("/appointments/{appointment_id}/confirm")
def confirm_appointment(appointment_id: str, body: ConfirmIn | None = None,
                        db: Session = Depends(get_db)) -> dict:
    """The only place the request email is sent. draft -> pending."""
    a = get_appointment(db, appointment_id)
    if a.status == S.pending:
        return status_view(a)                       # double click: already sent
    if a.status != S.draft:
        raise HTTPException(409, f"This request is {a.status.value}, so it can't be sent.")

    message = a.message
    if body and body.message is not None:
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "The message can't be empty.")

    check_slot(db, a.faculty_id, a.date, a.start, a.duration_min, exclude_id=a.id)

    faculty_email = ctx.directory.email_for(a.faculty_id)   # the list may have been updated
    if faculty_email is None and ctx.mailer.real:
        raise HTTPException(409, f"Booking isn't set up for {a.faculty_name} yet: we don't have their email.")

    now = clock.utcnow()
    try:
        claim(db, a, S.draft, status=S.pending, message=message, faculty_email=faculty_email,
              confirmed_at=now, expires_at=now + timedelta(hours=ctx.settings.token_ttl_hours))
    except HTTPException:
        db.refresh(a)
        return status_view(a)                       # a parallel click won the race
    token = mint_token(db, a.id, "faculty")
    db.commit()
    db.refresh(a)

    base = f"{ctx.settings.frontend_url}/respond/{a.id}?token={token}"
    links = {act: f"{base}&action={act}" for act in ("accept", "decline", "propose")}
    subject, html, text = emails.faculty_request(a, links)
    try:
        ctx.mailer.send(Mail(to=faculty_email or "unknown-faculty@invalid", subject=subject,
                             html=html, text=text, reply_to=a.student_email))
    except MailError as exc:
        # Put it back to a draft so the student can try again.
        a.status, a.confirmed_at, a.expires_at = S.draft, None, None
        _burn_tokens(db, a.id)
        db.commit()
        raise HTTPException(503, str(exc))
    return status_view(a)


@router.post("/appointments/{appointment_id}/cancel")
def cancel_appointment(appointment_id: str, body: CancelIn | None = None,
                       db: Session = Depends(get_db)) -> dict:
    """Cancel. A reason is required unless it's an unsent draft; it goes to the faculty member."""
    a = get_appointment(db, appointment_id)
    refresh_expiry(db, a)
    was = a.status
    allowed, needs_reason = can_cancel(a)
    if not allowed:
        if was == S.accepted:
            raise HTTPException(409, "This appointment has already happened, so it can't be cancelled.")
        raise HTTPException(409, f"This request is already {was.value}.")

    reason = " ".join(((body and body.reason) or "").split()) or None
    if needs_reason and (not reason or len(reason) < MIN_REASON):
        raise HTTPException(422, f"Tell {a.faculty_name} why you're cancelling (at least {MIN_REASON} characters).")

    claim(db, a, was, status=S.cancelled, cancel_reason=reason, cancelled_at=clock.utcnow())
    _burn_tokens(db, a.id)
    db.commit()
    db.refresh(a)
    if was in WAITING:
        notify(a.faculty_email, *emails.faculty_update(a, "withdrawn"))
    elif was == S.accepted:
        notify(a.faculty_email, *emails.faculty_update(a, "cancelled"))
    return status_view(a)


@router.post("/appointments/lookup")
def lookup_appointments(body: LookupIn, db: Session = Depends(get_db)) -> dict:
    """The "My appointments" list. The browser sends the ids it saved when booking.

    Unknown ids are skipped silently. Each item says which actions to offer,
    so the frontend never has to work that out itself.
    """
    out = []
    for appointment_id in dict.fromkeys(body.ids):          # de-duplicate, keep order
        a = db.get(Appointment, appointment_id)
        if a is None:
            continue
        refresh_expiry(db, a)
        cancel_ok, needs_reason = can_cancel(a)
        out.append({
            **status_view(a),
            "purpose": a.purpose,
            "created_at": a.created_at.isoformat(timespec="seconds") + "Z" if a.created_at else None,
            "is_past": datetime.combine(a.date, a.start) <= clock.now_ist(),
            "actions": {
                "cancel": cancel_ok,
                "cancel_needs_reason": needs_reason,
                "answer_proposal": a.status == S.rescheduled,
            },
        })
    out.sort(key=lambda x: x["slot"] or "", reverse=True)    # newest slot first
    return {"appointments": out}


@router.get("/appointments/{appointment_id}/status")
def appointment_status(appointment_id: str, db: Session = Depends(get_db)) -> dict:
    a = get_appointment(db, appointment_id)
    refresh_expiry(db, a)
    return status_view(a)


# ================================================================ faculty response page

@router.get("/appointments/{appointment_id}/respond")
def faculty_page(appointment_id: str, token: str = Query(min_length=10, max_length=200),
                 db: Session = Depends(get_db)) -> dict:
    """Data for the faculty page. Opening the link changes nothing."""
    a, _ = open_link(db, appointment_id, token, "faculty", expected=S.pending)
    return {
        "student": {"name": a.student_name, "roll_no": a.roll_no, "branch": a.branch,
                    "semester": a.semester, "section": a.section, "email": a.student_email},
        "faculty": a.faculty_name,
        "slot": iso_slot(a.date, a.start),
        "duration_min": a.duration_min,
        "purpose": a.purpose,
        "message": a.message,
        "status": a.status.value,
        "suggested_slots": suggested_slots(db, a),
    }


@router.post("/appointments/{appointment_id}/respond")
def faculty_respond(appointment_id: str, body: FacultyRespondIn, db: Session = Depends(get_db)) -> dict:
    a, tok = open_link(db, appointment_id, body.token, "faculty", expected=S.pending)
    note = (body.note or "").strip() or None
    now = clock.utcnow()

    if body.action == "accept":
        # Two requests for the same slot can both be pending; only one can be accepted.
        for s, e, other in held_slots(db, a.faculty_id, a.date, a.id):
            if other.status == S.accepted and overlaps(a.start, a.end, s, e):
                raise HTTPException(409, f"You've already accepted another appointment from "
                                         f"{emails.pretty_time(s)} to {emails.pretty_time(e)}.")
        values = dict(status=S.accepted, faculty_note=note, responded_at=now)
    elif body.action == "decline":
        values = dict(status=S.declined, faculty_note=note, responded_at=now)
    else:
        if not body.proposed_slot:
            raise HTTPException(422, "Pick a time to propose.")
        d, start = parse_slot(body.proposed_slot)
        if (d, start) == (a.date, a.start):
            raise HTTPException(422, "That's the time the student asked for. Choose Accept instead.")
        end = check_slot(db, a.faculty_id, d, start, a.duration_min, exclude_id=a.id)
        values = dict(status=S.rescheduled, faculty_note=note, responded_at=now,
                      proposed_date=d, proposed_start=start, proposed_end=end,
                      expires_at=now + timedelta(hours=ctx.settings.token_ttl_hours))

    spend_token(db, tok)
    claim(db, a, S.pending, **values)
    link = None
    if body.action == "propose":
        raw = mint_token(db, a.id, "student")
        link = f"{ctx.settings.frontend_url}/reschedule/{a.id}?token={raw}"
    db.commit()
    db.refresh(a)

    kind = {"accept": "accepted", "decline": "declined", "propose": "proposed"}[body.action]
    notify(a.student_email, *emails.student_update(a, kind, link))
    return {"status": a.status.value, "detail": "Done. The student has been notified."}


# ================================================================ student reschedule page

@router.get("/appointments/{appointment_id}/student-response")
def student_page(appointment_id: str, token: str = Query(min_length=10, max_length=200),
                 db: Session = Depends(get_db)) -> dict:
    a, _ = open_link(db, appointment_id, token, "student", expected=S.rescheduled)
    return {
        "faculty": a.faculty_name,
        "original_slot": iso_slot(a.date, a.start),
        "proposed_slot": iso_slot(a.proposed_date, a.proposed_start),
        "duration_min": a.duration_min,
        "faculty_note": a.faculty_note,
        "status": a.status.value,
    }


def _answer_proposal(db: Session, a: Appointment, action: str, tok: AppointmentToken | None) -> dict:
    now = clock.utcnow()
    if action == "accept":
        check_slot(db, a.faculty_id, a.proposed_date, a.proposed_start, a.duration_min, exclude_id=a.id)
        values = dict(status=S.accepted, student_responded_at=now,
                      date=a.proposed_date, start=a.proposed_start, end=a.proposed_end,
                      proposed_date=None, proposed_start=None, proposed_end=None)
    else:
        values = dict(status=S.declined, student_responded_at=now)

    if tok is not None:
        spend_token(db, tok)
    claim(db, a, S.rescheduled, **values)
    _burn_tokens(db, a.id)          # the email link can't be used after answering here, and vice versa
    db.commit()
    db.refresh(a)
    notify(a.faculty_email, *emails.faculty_update(a, "confirmed" if action == "accept" else "student_declined"))
    return status_view(a)


@router.post("/appointments/{appointment_id}/student-response")
def student_respond(appointment_id: str, body: StudentRespondIn, db: Session = Depends(get_db)) -> dict:
    """From the email link (needs the token)."""
    a, tok = open_link(db, appointment_id, body.token, "student", expected=S.rescheduled)
    return _answer_proposal(db, a, body.action, tok)


@router.post("/appointments/{appointment_id}/proposal")
def answer_proposal(appointment_id: str, body: ProposalAnswerIn, db: Session = Depends(get_db)) -> dict:
    """From the My appointments list. The id itself is the proof: only the
    student's browser has it."""
    a = get_appointment(db, appointment_id)
    refresh_expiry(db, a)
    if a.status != S.rescheduled:
        raise HTTPException(409, f"There's no new time to answer: this request is {a.status.value}.")
    return _answer_proposal(db, a, body.action, None)