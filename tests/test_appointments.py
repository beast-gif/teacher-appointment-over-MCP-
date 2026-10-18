"""Booking flow tests against the real EAC 7th sem timetable.

Run from the tt_mcp folder:  pytest -q
The clock is frozen at Thu 1 Oct 2026, 12:00 IST, so "Monday" is 5 Oct.
Dr. Bhavana V on Monday: class 09:50-10:40, free 11:00-12:40, 'maybe' 14:00-15:40, free 15:40-16:20.
"""
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from app.api import clock
from app.api.config import Settings
from app.api.context import ctx
from app.api.main import create_app
from app.api.mailer import GraphMailer

FIXTURE = Path(__file__).parent / "fixtures" / "eac_7th_sem.xlsx"
MON, TUE = "2026-10-05", "2026-10-06"


@pytest.fixture
def now(monkeypatch):
    state = {"ist": datetime(2026, 10, 1, 12, 0)}
    monkeypatch.setattr(clock, "now_ist", lambda: state["ist"])
    monkeypatch.setattr(clock, "utcnow", lambda: state["ist"] - timedelta(hours=5, minutes=30))
    return state


@pytest.fixture
def client(tmp_path, now):
    directory = tmp_path / "faculty_directory.csv"
    directory.write_text(
        "faculty_id,name,courses,email\n"
        "bhavana v,Dr. Bhavana V,23ECE445,bhavana@blr.amrita.edu\n"
        "sushma s,Dr. Sushma S,23ECE451,sushma@blr.amrita.edu\n"
        "deepa kumari,Dr. Deepa Kumari,24ENG243,\n",
        encoding="utf-8",
    )
    app = create_app(Settings(
        timetable_file=str(FIXTURE),
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        faculty_directory=str(directory),
        maildir=str(tmp_path / "mail"),
        frontend_url="http://front",
    ))
    return TestClient(app)


def student(**kw):
    s = {"name": "Rahul Sharma", "roll_no": "bl.en.u4eac23045", "branch": "EAC",
         "semester": 7, "section": "A", "email": "rahul@bl.students.amrita.edu"}
    s.update(kw)
    return s


def create(client, faculty_id="bhavana v", date=MON, time="11:00", **kw):
    body = {"faculty_id": faculty_id, "date": date, "time": time, "duration_min": 15,
            "student": student(**kw.pop("student", {})), "purpose": "Doubt on the DSP mini-project"}
    body.update(kw)
    return client.post("/appointments", json=body)


def sent():
    return [BytesParser(policy=policy.default).parsebytes(p.read_bytes()) for p in ctx.mailer.sent]


def html_of(msg):
    return msg.get_body(("html",)).get_content()


def token_from(msg, kind="respond"):
    m = re.search(rf"/{kind}/\w+\?token=([\w-]+)", msg.get_body(("plain",)).get_content())
    return m.group(1)


def confirmed(client, **kw):
    r = create(client, **kw)
    assert r.status_code == 200, r.text
    appt_id = r.json()["appointment_id"]
    assert client.post(f"/appointments/{appt_id}/confirm", json={}).json()["status"] == "pending"
    return appt_id, token_from(sent()[-1])


# ---------------------------------------------------------------- create

def test_create_returns_preview_and_sends_nothing(client):
    r = create(client, purpose="<script>alert(1)</script> filter design")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "draft"
    assert data["preview"]["to"] == "Dr. Bhavana V <bhavana@blr.amrita.edu>"
    assert "Rahul Sharma" in data["preview"]["body_html"]
    assert "BL.EN.U4EAC23045" in data["preview"]["message"]          # roll upper-cased
    assert "<script>" not in data["preview"]["body_html"]             # escaped
    assert "&lt;script&gt;" in data["preview"]["body_html"]
    assert ctx.mailer.sent == []


@pytest.mark.parametrize("date,time,expect", [
    (MON, "10:00", "isn't available"),           # class 23ECE445
    (MON, "12:45", "isn't available"),           # lunch break
    ("2026-10-10", "11:00", "isn't available"),  # Saturday
    (MON, "07:30", "isn't available"),           # before college hours
    ("2026-09-28", "11:00", "already passed"),
    ("2026-12-20", "11:00", "within the next"),
])
def test_bad_times_rejected(client, date, time, expect):
    r = create(client, date=date, time=time)
    assert r.status_code == 409 and expect in r.json()["detail"], r.text


def test_maybe_slot_is_bookable(client):
    assert create(client, time="14:00").status_code == 200    # Counselling slot: faculty decides


@pytest.mark.parametrize("change", [
    {"student": {"email": "rahul@gmail.com"}},
    {"student": {"roll_no": "12345"}},
    {"student": {"semester": 9}},
    {"duration_min": 60},
    {"purpose": "   "},
])
def test_validation(client, change):
    assert create(client, **change).status_code == 422


def test_unknown_faculty(client):
    assert create(client, faculty_id="dr nobody").status_code == 404


# ---------------------------------------------------------------- confirm

def test_confirm_sends_once_with_reply_to_and_edited_message(client):
    appt_id = create(client).json()["appointment_id"]
    r = client.post(f"/appointments/{appt_id}/confirm", json={"message": "Edited <b>by</b> me"})
    assert r.status_code == 200
    card = r.json()
    assert card["type"] == "booking_status" and card["status"] == "pending"
    assert card["slot"] == "2026-10-05T11:00:00+05:30"

    [msg] = sent()
    assert msg["To"] == "bhavana@blr.amrita.edu"
    assert msg["Reply-To"] == "rahul@bl.students.amrita.edu"
    html = html_of(msg)
    assert "Edited &lt;b&gt;by&lt;/b&gt; me" in html
    for action in ("accept", "decline", "propose"):
        assert f"http://front/respond/{appt_id}?token=" in html and f"action={action}" in html

    again = client.post(f"/appointments/{appt_id}/confirm", json={})
    assert again.json()["status"] == "pending"
    assert len(sent()) == 1                                    # double click: no second email


# ---------------------------------------------------------------- faculty accepts

def test_faculty_accept(client):
    appt_id, token = confirmed(client)

    for _ in range(2):                                         # opening the page twice changes nothing
        page = client.get(f"/appointments/{appt_id}/respond", params={"token": token})
        assert page.status_code == 200, page.text
    data = page.json()
    assert data["student"]["roll_no"] == "BL.EN.U4EAC23045"
    assert data["suggested_slots"] and all(s["date"] >= MON for s in data["suggested_slots"])

    r = client.post(f"/appointments/{appt_id}/respond",
                    json={"token": token, "action": "accept", "note": "Meet at cabin 204"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"

    student_mail = sent()[-1]
    assert student_mail["To"] == "rahul@bl.students.amrita.edu"
    assert "Meet at cabin 204" in html_of(student_mail)

    card = client.get(f"/appointments/{appt_id}/status").json()
    assert card["status"] == "accepted" and card["faculty_note"] == "Meet at cabin 204"

    again = client.post(f"/appointments/{appt_id}/respond", json={"token": token, "action": "decline"})
    assert again.status_code == 410
    assert client.get(f"/appointments/{appt_id}/respond", params={"token": token}).status_code == 410


# ---------------------------------------------------------------- faculty proposes, student accepts

def test_propose_then_student_accepts(client):
    appt_id, token = confirmed(client, time="15:45")

    busy = client.post(f"/appointments/{appt_id}/respond",
                       json={"token": token, "action": "propose", "proposed_slot": f"{MON}T10:00"})
    assert busy.status_code == 409                             # her own class
    missing = client.post(f"/appointments/{appt_id}/respond", json={"token": token, "action": "propose"})
    assert missing.status_code == 422

    r = client.post(f"/appointments/{appt_id}/respond",
                    json={"token": token, "action": "propose", "note": "I have a meeting",
                          "proposed_slot": f"{TUE}T10:00:00+05:30"})
    assert r.status_code == 200 and r.json()["status"] == "rescheduled"

    card = client.get(f"/appointments/{appt_id}/status").json()
    assert card["proposed_slot"] == "2026-10-06T10:00:00+05:30"

    student_token = token_from(sent()[-1], "reschedule")
    page = client.get(f"/appointments/{appt_id}/student-response", params={"token": student_token})
    assert page.status_code == 200 and page.json()["faculty_note"] == "I have a meeting"

    r = client.post(f"/appointments/{appt_id}/student-response",
                    json={"token": student_token, "action": "accept"})
    assert r.status_code == 200
    card = r.json()
    assert card["status"] == "accepted" and card["slot"] == "2026-10-06T10:00:00+05:30"
    assert card["proposed_slot"] is None
    assert sent()[-1]["To"] == "bhavana@blr.amrita.edu" and "Confirmed" in sent()[-1]["Subject"]

    reuse = client.post(f"/appointments/{appt_id}/student-response",
                        json={"token": student_token, "action": "decline"})
    assert reuse.status_code == 410


def test_student_declines_new_time(client):
    appt_id, token = confirmed(client)
    client.post(f"/appointments/{appt_id}/respond",
                json={"token": token, "action": "propose", "proposed_slot": f"{TUE}T10:00"})
    student_token = token_from(sent()[-1], "reschedule")
    r = client.post(f"/appointments/{appt_id}/student-response",
                    json={"token": student_token, "action": "decline"})
    assert r.json()["status"] == "declined"


# ---------------------------------------------------------------- clashes, cancel, links, expiry, limits

def test_same_slot_is_held_once_confirmed(client):
    confirmed(client)                                          # 11:00-11:15 pending
    other = {"name": "Asha", "roll_no": "BL.EN.U4EAC23001", "email": "asha@bl.students.amrita.edu"}
    clash = create(client, time="11:10", student=other)
    assert clash.status_code == 409 and "already has an appointment" in clash.json()["detail"]
    assert create(client, time="11:15", student=other).status_code == 200


def test_cancel(client):
    draft_id = create(client).json()["appointment_id"]
    assert client.post(f"/appointments/{draft_id}/cancel").json()["status"] == "cancelled"   # draft: no reason needed
    assert sent() == []                                        # draft: faculty never knew

    appt_id, token = confirmed(client, time="11:30")
    no_reason = client.post(f"/appointments/{appt_id}/cancel")
    assert no_reason.status_code == 422 and "why" in no_reason.json()["detail"]
    too_short = client.post(f"/appointments/{appt_id}/cancel", json={"reason": "  no "})
    assert too_short.status_code == 422

    r = client.post(f"/appointments/{appt_id}/cancel", json={"reason": "I have a lab viva then"})
    assert r.json()["status"] == "cancelled" and r.json()["cancel_reason"] == "I have a lab viva then"
    mail = sent()[-1]
    assert "Withdrawn" in mail["Subject"] and "I have a lab viva then" in html_of(mail)
    page = client.get(f"/appointments/{appt_id}/respond", params={"token": token})
    assert page.status_code == 410 and "cancelled" in page.json()["detail"]
    assert client.post(f"/appointments/{appt_id}/cancel", json={"reason": "again please"}).status_code == 409


def test_cancel_accepted_appointment(client, now):
    appt_id, token = confirmed(client)
    client.post(f"/appointments/{appt_id}/respond", json={"token": token, "action": "accept"})
    assert client.post(f"/appointments/{appt_id}/cancel").status_code == 422          # reason required
    r = client.post(f"/appointments/{appt_id}/cancel", json={"reason": "Doubt got cleared in class"})
    assert r.json()["status"] == "cancelled"
    mail = sent()[-1]
    assert mail["To"] == "bhavana@blr.amrita.edu" and "Cancelled" in mail["Subject"]
    assert "Doubt got cleared in class" in html_of(mail)
    # the slot is free again
    other = {"name": "Asha", "roll_no": "BL.EN.U4EAC23001", "email": "asha@bl.students.amrita.edu"}
    assert create(client, student=other).status_code == 200


def test_cannot_cancel_after_it_happened(client, now):
    appt_id, token = confirmed(client)
    client.post(f"/appointments/{appt_id}/respond", json={"token": token, "action": "accept"})
    now["ist"] = datetime(2026, 10, 5, 12, 0)                 # after Monday 11:00
    r = client.post(f"/appointments/{appt_id}/cancel", json={"reason": "too late now"})
    assert r.status_code == 409 and "already happened" in r.json()["detail"]


def test_lookup_my_appointments(client):
    draft_id = create(client, time="15:45").json()["appointment_id"]
    pending_id, _ = confirmed(client)
    proposed_id, token = confirmed(client, time="11:30")
    client.post(f"/appointments/{proposed_id}/respond",
                json={"token": token, "action": "propose", "proposed_slot": f"{TUE}T10:00"})

    r = client.post("/appointments/lookup", json={"ids": [pending_id, "nope", draft_id, proposed_id, pending_id]})
    assert r.status_code == 200
    items = {a["appointment_id"]: a for a in r.json()["appointments"]}
    assert set(items) == {draft_id, pending_id, proposed_id}          # unknown skipped, duplicates merged
    assert items[draft_id]["actions"] == {"cancel": True, "cancel_needs_reason": False, "answer_proposal": False}
    assert items[pending_id]["actions"] == {"cancel": True, "cancel_needs_reason": True, "answer_proposal": False}
    assert items[proposed_id]["actions"]["answer_proposal"] is True
    assert items[proposed_id]["proposed_slot"] == "2026-10-06T10:00:00+05:30"
    assert items[pending_id]["purpose"] == "Doubt on the DSP mini-project"
    assert client.post("/appointments/lookup", json={"ids": ["x"] * 51}).status_code == 422


def test_answer_proposal_from_the_list(client):
    appt_id, token = confirmed(client)
    client.post(f"/appointments/{appt_id}/respond",
                json={"token": token, "action": "propose", "proposed_slot": f"{TUE}T10:00"})
    student_token = token_from(sent()[-1], "reschedule")

    r = client.post(f"/appointments/{appt_id}/proposal", json={"action": "accept"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted" and r.json()["slot"] == "2026-10-06T10:00:00+05:30"
    assert "Confirmed" in sent()[-1]["Subject"]
    # the email link no longer works, and answering twice is refused
    link = client.get(f"/appointments/{appt_id}/student-response", params={"token": student_token})
    assert link.status_code == 410
    assert client.post(f"/appointments/{appt_id}/proposal", json={"action": "decline"}).status_code == 409


def test_bad_links(client):
    first, token = confirmed(client)
    second, _ = confirmed(client, time="11:30")
    wrong_appt = client.get(f"/appointments/{second}/respond", params={"token": token})
    assert wrong_appt.status_code == 410 and "isn't valid" in wrong_appt.json()["detail"]
    junk = client.get(f"/appointments/{first}/respond", params={"token": "x" * 40})
    assert junk.status_code == 410
    student_kind = client.get(f"/appointments/{first}/student-response", params={"token": token})
    assert student_kind.status_code == 410


def test_expiry_after_24_hours(client, now):
    appt_id, token = confirmed(client, date="2026-10-08", time="09:00")   # Thursday 8 Oct
    now["ist"] += timedelta(hours=25)
    assert client.get(f"/appointments/{appt_id}/status").json()["status"] == "expired"
    page = client.get(f"/appointments/{appt_id}/respond", params={"token": token})
    assert page.status_code == 410 and "expired" in page.json()["detail"]


def test_expires_when_the_time_passes(client, now):
    appt_id, _ = confirmed(client)                 # Monday 11:00
    now["ist"] = datetime(2026, 10, 5, 11, 1)
    assert client.get(f"/appointments/{appt_id}/status").json()["status"] == "expired"


def test_limit_on_waiting_requests(client):
    for t in ("11:00", "11:30", "12:00"):
        confirmed(client, time=t)
    r = create(client, time="15:45")
    assert r.status_code == 429


def test_real_mailer_needs_a_known_email(client):
    ctx.mailer = GraphMailer()
    no_email = create(client, faculty_id="deepa kumari", time="11:00")
    assert no_email.status_code == 409 and "don't have their email" in no_email.json()["detail"]

    appt_id = create(client).json()["appointment_id"]          # Bhavana has an email
    r = client.post(f"/appointments/{appt_id}/confirm", json={})
    assert r.status_code == 503                                # Graph not set up yet
    assert client.get(f"/appointments/{appt_id}/status").json()["status"] == "draft"   # can retry


def test_old_database_gets_new_columns(tmp_path):
    """A faculty.db made before cancel_reason existed must upgrade itself, keeping its rows."""
    import sqlite3
    from app.db.session import init_db

    db_file = tmp_path / "old.db"
    init_db(f"sqlite:///{db_file}")
    con = sqlite3.connect(db_file)
    con.execute("ALTER TABLE appointments DROP COLUMN cancel_reason")
    con.execute("ALTER TABLE appointments DROP COLUMN cancelled_at")
    con.execute("INSERT INTO appointments (id, status, student_name, roll_no, branch, semester, student_email, "
                "faculty_id, faculty_name, date, start, \"end\", duration_min, purpose, message, created_at) VALUES "
                "('old1', 'pending', 'A', 'BL.EN.X', 'EAC', 7, 'a@x', 'bhavana v', 'Dr. Bhavana V', "
                "'2026-10-05', '11:00:00.000000', '11:15:00.000000', 15, 'p', 'm', '2026-10-01 06:30:00')")
    con.commit()
    con.close()

    init_db(f"sqlite:///{db_file}")                         # what happens on the next start
    con = sqlite3.connect(db_file)
    cols = {row[1] for row in con.execute("PRAGMA table_info(appointments)")}
    assert {"cancel_reason", "cancelled_at"} <= cols
    assert con.execute("SELECT id, status FROM appointments").fetchall() == [("old1", "pending")]