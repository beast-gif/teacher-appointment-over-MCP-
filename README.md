# tt_mcp — Faculty availability and appointment booking

The timetable and booking service behind the **Faculty desk** in the Triage chatbot
(Amrita Vishwa Vidyapeetham, Bengaluru).

A student can ask the chatbot:

- **"What's on Tuesday?"** → the class timetable (no booking offered)
- **"Is Dr. X free Monday at 11?"** → free / busy / maybe, other free times, and a **Book** button

Booking sends the faculty member an email with **Accept / Decline / Reschedule** buttons.
The student sees the answer live in the chat, and can manage everything from **My appointments**.

---

## How it fits together

```
React frontend (Vite)                 Triage backend :7860                 tt_mcp :8000  (this repo)
──────────────────────                ─────────────────────                ─────────────────────────
chat, booking form,      ── HTTP ──►  /desk/chat ── LLM ── MCP client ──►  /mcp           MCP server (5 tools)
faculty /respond page,                /desk/appointments/*  ── forward ──►  /appointments  booking API
student /reschedule page                       (desk_booking.py)           │
                                                                           ├── timetable (Excel, parsed at start)
                                                                           ├── database (SQLite / Postgres)
                                                                           └── email (file / SMTP / Graph)
```

- The **frontend only talks to Triage**. Triage forwards booking requests to tt_mcp unchanged.
- The **LLM never books anything**. It can only call the read-only MCP tools. Emails go out
  only when a person clicks a button (student: *Confirm and send*; faculty: *Accept* etc.).
- The MCP server and the booking API run in **one process, on one port**, sharing one copy of
  the timetable and the bookings database. So the chat never offers a slot that is already booked.

---

## Quick start

Requires Python 3.11+.

```bash
cd tt_mcp
python -m venv venv
venv\Scripts\activate            # Windows   (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
```

Put these in the `tt_mcp` folder:

| File | What it is |
|---|---|
| `EAC_7th_sem_timetable.xlsx` | The class timetable |
| `name_fixes.json` | Full names for names cut off in the timetable |
| `faculty_directory.csv` | Faculty email addresses (see below) |

Run:

```bash
uvicorn app.api.main:create_app --factory --reload
```

| URL | What |
|---|---|
| http://127.0.0.1:8000/docs | Try every booking route |
| http://127.0.0.1:8000/mcp | The MCP server |
| http://127.0.0.1:8000/health | Quick check: faculty loaded, emails known, email mode |

Then start Triage on port **7860** and the frontend with `npm run dev`.
Start tt_mcp **first**, so Triage connects to `/mcp` at startup.

---

## Folder structure

```
tt_mcp/
├── EAC_7th_sem_timetable.xlsx      timetable read at startup
├── name_fixes.json                 {"Dr. A. Kum...": "Dr. A. Kumar"}
├── faculty_directory.csv           faculty_id, name, courses, email
├── requirements.txt
│
├── app/
│   ├── server.py                   MCP server: the 5 tools the LLM can call
│   ├── availability.py             free / busy / maybe, free slots, fuzzy name lookup
│   ├── directory.py                loads faculty_directory.csv (python -m app.directory makes a template)
│   │
│   ├── ingestion/                  reads the Excel timetable
│   │   ├── grid.py                 sheet → grid, merged cells resolved
│   │   ├── text.py                 days, times, course codes, names, rooms, credits
│   │   ├── reader.py               finds days, periods, breaks, lab slots, subject legend
│   │   ├── extract.py              grid → class blocks (electives grouped, lab timings)
│   │   └── faculty.py              blocks → each faculty member's week
│   │
│   ├── db/
│   │   ├── models.py               tables (appointments, tokens, timetable ...)
│   │   └── session.py              engine; adds new columns to an existing database
│   │
│   └── api/                        FastAPI app
│       ├── main.py                 creates the app, mounts the MCP server at /mcp
│       ├── appointments.py         all /appointments routes
│       ├── emails.py               email content (HTML + plain text)
│       ├── mailer.py               sending: file / smtp / graph
│       ├── config.py               settings from environment variables
│       ├── context.py              shared state
│       ├── clock.py                "now" in IST (frozen in tests)
│       └── show_links.py           prints the links from the newest test emails
│
└── tests/
    ├── fixtures/eac_7th_sem.xlsx
    └── test_appointments.py        33 tests
```

Created at runtime (git-ignored): `faculty.db`, `sent_emails/`.

---

## The timetable

The parser reads the sheet by **searching** for its parts, not by fixed cell positions:

| Part | How it's found | EAC 7th sem |
|---|---|---|
| Days | column with the most day names | column A, Monday–Friday |
| Periods | row above the days with the most `8:10-9:00`-style ranges | row 5, 8 periods, 08:10–16:20 |
| Breaks | header cells containing *break / lunch* | tea 10:40–11:00, lunch 12:40–14:00 |
| Lab slots | a row labelled *Lab* between header and Monday | 08:10–10:25, 10:50–13:05, 13:25–15:40 |
| Legend | header row with both *code* and *faculty* | 16 courses, 24 faculty |

Rules worth knowing:

- A bare **`2:00` means 14:00** (hours below 7 are read as pm).
- An **elective cell** like `23ECE341/23ECE451/23ECE436` makes every faculty member of those courses busy.
- A **lab** that covers a whole lab slot uses the lab slot's times.
- **Evaluation / Counselling** slots are **"maybe"**: bookable, and the faculty member confirms.
  Change this in `ACTIVITY_RULES` in `app/ingestion/faculty.py`.
- Names cut off in the sheet (`Dr. A. Kum...`) are fixed through `name_fixes.json`.

Check what the parser understood:

```bash
python -m app.ingestion.reader  EAC_7th_sem_timetable.xlsx    # days, periods, labs, legend
python -m app.ingestion.extract EAC_7th_sem_timetable.xlsx    # every class block
python -m app.ingestion.faculty EAC_7th_sem_timetable.xlsx    # each faculty member's week
python -m app.availability EAC_7th_sem_timetable.xlsx "Kumar sir" Monday 10:00
```

---

## Faculty emails

`faculty_directory.csv` maps each faculty member to an email. Make a template from the timetable:

```bash
python -m app.directory EAC_7th_sem_timetable.xlsx      # won't overwrite an existing file
```

```csv
faculty_id,name,courses,email
a kumar,Dr. A. Kumar,23ECE445,a.kumar@college.edu
```

- Fill in **only** the `email` column. `faculty_id` is how rows match the timetable.
- A blank email means *unknown*: the backend **never guesses an address**.
  With real sending on, booking that person is refused with a clear message.
- Restart tt_mcp after editing.

---

## MCP tools

Served at `/mcp` (streamable HTTP). Used by Triage's `/desk/chat` through its MCP client.

| Tool | Use it for | Example result |
|---|---|---|
| `check_faculty_availability(faculty_name, day, time, duration_minutes=15)` | "Is X free at …?" | `busy`, "Class 23ECE445 from 09:50 to 10:40", alternatives, `bookable: false` |
| `get_free_slots(faculty_name, day)` | "When is X free on …?" | `[{"start": "11:00", "end": "12:40"}, ...]` |
| `get_timetable(day?, faculty_name?)` | timetable questions (never offers booking) | classes with titles, faculty, rooms |
| `find_faculty(name)` | checking a name | `found` / `did_you_mean` / `ambiguous` / `not_found` |
| `get_today()` | resolving "tomorrow" | `{"date": "2026-10-03", "day": "Saturday"}` |

- `day` accepts `today`, `tomorrow`, `Monday`, or `2026-10-05`. `time` accepts `10:00`, `2pm`, `14:30`.
- Names are fuzzy: `"Kumar ma'am"`, `"Kumar sir"`, a typo like `"kumr"` → *did you mean Dr. A. Kumar?*
- Availability statuses: `free`, `busy`, `maybe`, `break`, `outside_hours`. `bookable` is true only for `free` and `maybe`.
- **Booked appointments count as busy.** Pending, accepted and proposed times are cut out of
  free slots: a free window 15:40–16:20 with a booking 15:40–15:55 becomes **15:55–16:20**.

Run the MCP server on its own (rarely needed):

```bash
python -m app.server            # stdio, e.g. for MCP Inspector
python -m app.server --http     # HTTP on :8000/mcp — don't run this alongside uvicorn
```

---

## Booking API

Through Triage these are under `/desk` (e.g. `/desk/appointments`).

| Method | Route | Who | What |
|---|---|---|---|
| POST | `/appointments` | student | create a **draft** + email preview. Sends nothing |
| POST | `/appointments/{id}/confirm` | student | **the only place the request email is sent**. draft → pending |
| POST | `/appointments/{id}/cancel` | student | cancel, **reason required** (except unsent drafts) |
| GET | `/appointments/{id}/status` | chat | booking status card, polled every 30 s |
| POST | `/appointments/lookup` | student | **My appointments**: status of the ids saved in the browser |
| POST | `/appointments/{id}/proposal` | student | accept / decline a proposed new time from My appointments |
| GET | `/appointments/{id}/respond?token=` | faculty | page data. **Opening it changes nothing** |
| POST | `/appointments/{id}/respond` | faculty | accept / decline / reschedule (`action: "propose"`) + optional note |
| GET | `/appointments/{id}/student-response?token=` | student | reschedule page data. Changes nothing |
| POST | `/appointments/{id}/student-response` | student | accept / decline the new time from the email link |

Errors always come back as `{"detail": "readable message"}`:
`404` unknown, `409` slot taken / not allowed now, `410` link invalid, used or expired,
`422` bad input, `429` too many waiting requests, `503` email couldn't be sent.

### Lifecycle

```
draft ──confirm──► pending ──faculty accepts──► accepted
  │                  │  │
cancel            decline  reschedule
  ▼                  ▼        ▼
cancelled        declined   rescheduled ──student accepts──► accepted (at the new time)
                               │
                        student declines ──► declined

pending / rescheduled: expire after 24 h, or when the time passes
pending / rescheduled / accepted: student can cancel, with a reason
```

### Rules the backend enforces (not the frontend, not the LLM)

- **Valid slot only:** not in the past, within 30 days, within college hours (Mon–Fri 08:10–16:20),
  not during a class or break, and not overlapping another pending or accepted appointment.
  Checked when the draft is created **and again** just before sending.
- **Student details:** roll number like `BL.EN.U4EAC23051`, email must end in `@bl.students.amrita.edu`,
  semester 1–8, duration 15 / 30 / 45 minutes, purpose up to 300 characters.
- **At most 3 requests waiting** for a reply per roll number.
- **Faculty name and email come from the server's data**, never from the browser.
- **Links are single-use:** random tokens, stored only as SHA-256 hashes, 24-hour expiry.
  Opening a link never uses it; only submitting does. (Outlook's link scanner opens links automatically.)
- **No double sends:** status changes are atomic, so a double click on *Confirm* or *Accept* sends one email.
- **Reply-To** on the faculty email is the student, so *Reply* reaches them.
- Everything from the student is **HTML-escaped** in emails.
- Appointment ids are random 32-character strings: the id itself is what lets a browser poll or cancel.

---

## Email

| `EMAIL_MODE` | Behaviour |
|---|---|
| `file` *(default)* | Each email is written as a `.eml` file in `sent_emails/`. Double-click to view |
| `smtp` | Really sends, e.g. Gmail with an app password |
| `graph` | Microsoft Graph / Outlook — **not implemented yet** (needs college IT approval); fails loudly |

Gmail:

```powershell
$env:EMAIL_MODE="smtp"
$env:SMTP_USER="you@gmail.com"
$env:SMTP_PASSWORD="your-16-char-app-password"
$env:MAIL_FROM="Campus Assistant <you@gmail.com>"
uvicorn app.api.main:create_app --factory --reload
```

Get the links out of test emails (they're encoded inside `.eml` files):

```bash
python -m app.api.show_links        # newest email
python -m app.api.show_links 3      # newest 3
```

---

## Settings

All optional; defaults work locally.

| Variable | Default | |
|---|---|---|
| `TIMETABLE_FILE` | `EAC_7th_sem_timetable.xlsx` | |
| `DATABASE_URL` | `sqlite:///./faculty.db` | Postgres on Railway (`postgres://` is accepted) |
| `FACULTY_DIRECTORY` | `faculty_directory.csv` | |
| `FRONTEND_URL` | `http://localhost:5173` | used to build the links in emails |
| `CORS_ORIGINS` | `http://localhost:5173` | comma-separated |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,[::1]:*` | **add your public domain when deployed**, or `/mcp` returns 421 |
| `EMAIL_MODE` | `file` | `file` / `smtp` / `graph` |
| `MAILDIR` | `sent_emails` | |
| `MAIL_FROM` | `Campus Assistant <no-reply@localhost>` | |
| `SMTP_HOST`, `SMTP_PORT` | `smtp.gmail.com`, `587` | |
| `SMTP_USER`, `SMTP_PASSWORD` | — | required for `smtp` |
| `ALLOWED_EMAIL_DOMAIN` | `bl.students.amrita.edu` | student emails must end with this |
| `TOKEN_TTL_HOURS` | `24` | link and request expiry |
| `MAX_WAITING_PER_STUDENT` | `3` | |
| `MAX_DAYS_AHEAD` | `30` | |

On the Triage side: `BOOKING_URL` (default `http://127.0.0.1:8000`) tells `desk_booking.py` where tt_mcp runs.

---

## Database

- SQLite file `faculty.db` locally; Postgres in production via `DATABASE_URL`.
- Tables are created on startup. **New nullable columns are added automatically** to an
  existing database, so upgrading the code doesn't require deleting it.
- **Reset all test bookings:** stop tt_mcp, delete `faculty.db` (and `sent_emails/`), start again.

---

## Tests

```bash
pytest -q          # 33 passed
```

The clock is frozen at Thu 1 Oct 2026, 12:00 IST, against the real EAC timetable. Covered:
drafts and previews, every invalid-slot case, validation, single send, faculty accept / decline /
reschedule, student answers by link and from the list, clashes, cancel with reason (including
accepted), expiry, link misuse, limits, real-mailer safeguards, database upgrade, and chat
availability that respects bookings.

---

## Deployment (Railway + Vercel)

1. **tt_mcp** on Railway with a Postgres add-on:
   `DATABASE_URL`, `FRONTEND_URL=https://<frontend>.vercel.app`,
   `CORS_ORIGINS=https://<frontend>.vercel.app`,
   `MCP_ALLOWED_HOSTS=<tt-mcp>.up.railway.app`, email settings.
   Start command: `uvicorn app.api.main:create_app --factory --host 0.0.0.0 --port $PORT`
2. **Triage** on Railway: `BOOKING_URL=https://<tt-mcp>.up.railway.app`, and its MCP URL
   pointing at `https://<tt-mcp>.up.railway.app/mcp`.
3. **Frontend** on Vercel: `VITE_API_URL=https://<triage>.up.railway.app`, `VITE_USE_MOCK_API=false`.

Put `faculty_directory.csv`, `name_fixes.json` and the timetable in the deployed tt_mcp folder.

---

## Known limitations / next steps

- **One timetable only (EAC 7th sem).** A faculty member's classes in other sections aren't known,
  so they can look free when they aren't. Next: load every section's timetable.
- **Outlook (Microsoft Graph)** sending isn't written yet; use `file` or `smtp`.
- **My appointments is per browser:** the list comes from ids saved in that browser.
  A verified-by-email version would work across devices.
- Times are IST; the college week is Mon–Fri.
