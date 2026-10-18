"""Email content. Every value from a form is HTML-escaped before it goes in.

Layout is table-based with inline styles because Outlook ignores most CSS.
Each function returns (subject, html, text).
"""
from __future__ import annotations

from datetime import date, time
from html import escape

from ..db.models import Appointment

ACCENT = "#185FA5"


# ---------------------------------------------------------------- formatting

def pretty_date(d: date) -> str:
    """2026-10-05 -> Mon, 5 Oct 2026"""
    return f"{d:%a}, {d.day} {d:%b %Y}"


def pretty_time(t: time) -> str:
    """14:30 -> 2:30 pm"""
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def pretty_slot(d: date, start: time, end: time) -> str:
    return f"{pretty_date(d)}, {pretty_time(start)} to {pretty_time(end)}"


def draft_message(student_name: str, roll_no: str, purpose: str) -> str:
    """The editable note the student sees on the preview."""
    purpose = purpose.strip().rstrip(".")
    return (
        f"I'm {student_name} ({roll_no}). I'd like to meet you about: {purpose}. "
        "Please let me know if this time works for you. Thank you."
    )


# ---------------------------------------------------------------- building blocks

def _e(value) -> str:
    return escape(str(value or ""), quote=True)


def _para(text: str) -> str:
    """Escape, then keep the student's line breaks."""
    return _e(text).replace("\n", "<br>")


def _button(href: str | None, label: str, primary: bool = False) -> str:
    bg, fg = (ACCENT, "#ffffff") if primary else ("#ffffff", ACCENT)
    href_attr = f'href="{_e(href)}"' if href else ""
    return (
        f'<a {href_attr} style="display:inline-block;padding:10px 18px;margin:0 8px 8px 0;'
        f"border:1px solid {ACCENT};border-radius:6px;background:{bg};color:{fg};"
        f'font-weight:bold;text-decoration:none;font-size:14px">{_e(label)}</a>'
    )


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "".join(
        f'<tr><td style="padding:3px 16px 3px 0;color:#666;white-space:nowrap">{_e(k)}</td>'
        f"<td style=\"padding:3px 0\">{_e(v)}</td></tr>"
        for k, v in pairs if v
    )


def _layout(body: str, footer: str) -> str:
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0">'
        '<tr><td align="center">'
        '<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;'
        'border:1px solid #e2e2e2;border-radius:8px;font-family:Arial,Helvetica,sans-serif;'
        'font-size:14px;line-height:1.6;color:#222">'
        f'<tr><td style="padding:24px">{body}</td></tr>'
        f'<tr><td style="padding:12px 24px;border-top:1px solid #eee;color:#888;font-size:12px">{footer}</td></tr>'
        "</table></td></tr></table>"
    )


def _slot_box(a: Appointment, d: date, start: time, end: time, extra: list[tuple[str, str]]) -> str:
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8fa;border-radius:6px;margin:12px 0">'
        f'<tr><td style="padding:12px 16px"><div style="font-weight:bold;margin-bottom:6px">'
        f"{_e(pretty_slot(d, start, end))} ({a.duration_min} min)</div>"
        f'<table cellpadding="0" cellspacing="0" style="font-size:13px">{_rows(extra)}</table>'
        "</td></tr></table>"
    )


def _student_rows(a: Appointment) -> list[tuple[str, str]]:
    sem = f"Semester {a.semester}" + (f", Section {a.section}" if a.section else "")
    return [
        ("Student", a.student_name),
        ("Roll number", a.roll_no),
        ("Branch", a.branch),
        ("Class", sem),
        ("Email", a.student_email),
    ]


# ---------------------------------------------------------------- to faculty

def faculty_request(a: Appointment, links: dict[str, str] | None) -> tuple[str, str, str]:
    """The request email. links=None renders the preview (buttons without links)."""
    slot = pretty_slot(a.date, a.start, a.end)
    subject = f"Appointment request: {a.student_name}, {pretty_date(a.date)}, {pretty_time(a.start)}"
    links = links or {}

    body = (
        f"<p>Dear {_e(a.faculty_name)},</p>"
        "<p>A student has requested an appointment with you. "
        "Your timetable shows no class at this time.</p>"
        + _slot_box(a, a.date, a.start, a.end, _student_rows(a))
        + f'<p style="margin:0;color:#666;font-size:13px">Purpose</p><p style="margin:0 0 12px">{_e(a.purpose)}</p>'
        + '<p style="margin:0;color:#666;font-size:13px">Message from the student</p>'
        + f'<p style="margin:0 0 20px;padding-left:12px;border-left:3px solid #ddd">{_para(a.message)}</p>'
        + _button(links.get("accept"), "Accept", primary=True)
        + _button(links.get("decline"), "Decline")
        + _button(links.get("propose"), "Reschedule")
        + '<p style="color:#666;font-size:13px;margin:8px 0 0">Each button opens a page where you can add a note before submitting.</p>'
    )
    if not links:
        body += ('<p style="color:#a35f00;font-size:13px">Preview: the buttons become live links '
                 "when the student confirms.</p>")
    footer = ("This request expires in 24 hours if there's no response. The links work once and only for you. "
              "Sent by the campus chatbot on the student's behalf. Replying to this email reaches the student.")

    text = "\n".join([
        f"Dear {a.faculty_name},",
        "",
        "A student has requested an appointment with you. Your timetable shows no class at this time.",
        "",
        f"When:        {slot} ({a.duration_min} min)",
        f"Student:     {a.student_name}",
        f"Roll number: {a.roll_no}",
        f"Branch:      {a.branch}",
        f"Class:       Semester {a.semester}" + (f", Section {a.section}" if a.section else ""),
        f"Email:       {a.student_email}",
        f"Purpose:     {a.purpose}",
        "",
        "Message from the student:",
        a.message,
        "",
        *( [f"Accept:      {links['accept']}",
            f"Decline:     {links['decline']}",
            f"Reschedule:  {links['propose']}"] if links else ["(Links are added when the student confirms.)"] ),
        "",
        "This request expires in 24 hours. The links work once. Replying reaches the student.",
    ])
    return subject, _layout(body, footer), text


def faculty_update(a: Appointment, kind: str) -> tuple[str, str, str]:
    """kind: 'confirmed' (student took the new time), 'student_declined',
    'withdrawn' (cancelled while waiting), 'cancelled' (cancelled after it was accepted)."""
    slot = pretty_slot(a.date, a.start, a.end)
    if kind == "confirmed":
        subject = f"Confirmed: {a.student_name}, {pretty_date(a.date)}, {pretty_time(a.start)}"
        line = f"{a.student_name} accepted the time you proposed. The appointment is confirmed for {slot}."
    elif kind == "student_declined":
        subject = f"Declined: {a.student_name} can't make the proposed time"
        line = f"{a.student_name} couldn't make the time you proposed, so this request is closed."
    elif kind == "cancelled":
        subject = f"Cancelled: appointment with {a.student_name}, {pretty_date(a.date)}, {pretty_time(a.start)}"
        line = f"{a.student_name} cancelled the appointment you accepted for {slot}. That time is free again."
    else:
        subject = f"Withdrawn: appointment request from {a.student_name}"
        line = f"{a.student_name} withdrew their request for {slot}. No action is needed."

    reason = getattr(a, "cancel_reason", None) if kind in ("cancelled", "withdrawn") else None
    reason_html = (
        f'<p style="margin:0;color:#666;font-size:13px">Reason given</p>'
        f'<p style="margin:0 0 12px;padding-left:12px;border-left:3px solid #ddd">{_para(reason)}</p>'
        if reason else ""
    )
    body = (f"<p>Dear {_e(a.faculty_name)},</p><p>{_e(line)}</p>{reason_html}"
            + _slot_box(a, a.date, a.start, a.end, _student_rows(a)))
    text = (f"Dear {a.faculty_name},\n\n{line}\n"
            + (f"\nReason given: {reason}\n" if reason else "")
            + f"\nStudent: {a.student_name} ({a.roll_no})")
    return subject, _layout(body, "Sent by the campus chatbot."), text


# ---------------------------------------------------------------- to the student

def student_update(a: Appointment, kind: str, reschedule_link: str | None = None) -> tuple[str, str, str]:
    """kind: 'accepted', 'declined', 'proposed'."""
    note = (
        f'<p style="margin:0;color:#666;font-size:13px">Note from {_e(a.faculty_name)}</p>'
        f'<p style="margin:0 0 12px;padding-left:12px;border-left:3px solid #ddd">{_para(a.faculty_note)}</p>'
        if a.faculty_note else ""
    )
    note_text = f"\nNote from {a.faculty_name}: {a.faculty_note}\n" if a.faculty_note else ""

    if kind == "accepted":
        subject = f"Accepted: your appointment with {a.faculty_name}"
        lead = f"{a.faculty_name} accepted your appointment."
        box = _slot_box(a, a.date, a.start, a.end, [])
        body = f"<p>Hi {_e(a.student_name)},</p><p>{_e(lead)}</p>{box}{note}"
        text = f"Hi {a.student_name},\n\n{lead}\nWhen: {pretty_slot(a.date, a.start, a.end)}\n{note_text}"
    elif kind == "declined":
        subject = f"Declined: your appointment request to {a.faculty_name}"
        lead = f"{a.faculty_name} can't meet at the time you asked for."
        body = (f"<p>Hi {_e(a.student_name)},</p><p>{_e(lead)}</p>{note}"
                "<p>You can ask the chatbot when they're free and send a new request.</p>")
        text = f"Hi {a.student_name},\n\n{lead}\n{note_text}\nAsk the chatbot for another time."
    else:
        subject = f"New time proposed by {a.faculty_name}"
        lead = f"{a.faculty_name} can't make your requested time and proposed another one."
        box = _slot_box(a, a.proposed_date, a.proposed_start, a.proposed_end,
                        [("Original request", pretty_slot(a.date, a.start, a.end))])
        body = (f"<p>Hi {_e(a.student_name)},</p><p>{_e(lead)}</p>{box}{note}"
                + _button(reschedule_link, "Accept or decline the new time", primary=True))
        text = (f"Hi {a.student_name},\n\n{lead}\n"
                f"Proposed: {pretty_slot(a.proposed_date, a.proposed_start, a.proposed_end)}\n"
                f"{note_text}\nAccept or decline: {reschedule_link}\n\nThe link works once and expires in 24 hours.")
    footer = "Sent by the campus chatbot." + (" The link works once and expires in 24 hours." if kind == "proposed" else "")
    return subject, _layout(body, footer), text