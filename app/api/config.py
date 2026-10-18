"""Settings, read from environment variables (all have local defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


@dataclass
class Settings:
    timetable_file: str = "EAC_7th_sem_timetable.xlsx"
    database_url: str = "sqlite:///./faculty.db"
    faculty_directory: str = "faculty_directory.csv"

    # Where the React app runs: used to build the links in emails.
    frontend_url: str = "http://localhost:5173"
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])

    # "file" writes .eml files (default), "smtp" really sends, "graph" is for later.
    email_mode: str = "file"
    maildir: str = "sent_emails"
    mail_from: str = "Campus Assistant <no-reply@localhost>"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # Host names the /mcp endpoint answers to. Add your Railway domain when deployed,
    # e.g. MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,yourapp.up.railway.app
    mcp_allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"])

    allowed_email_domain: str = "bl.students.amrita.edu"
    token_ttl_hours: int = 24
    max_waiting_per_student: int = 3     # pending + rescheduled requests at once
    max_days_ahead: int = 30

    @classmethod
    def from_env(cls) -> Settings:
        d = cls()
        return cls(
            timetable_file=_env("TIMETABLE_FILE", d.timetable_file),
            database_url=_env("DATABASE_URL", d.database_url),
            faculty_directory=_env("FACULTY_DIRECTORY", d.faculty_directory),
            frontend_url=_env("FRONTEND_URL", d.frontend_url).rstrip("/"),
            cors_origins=[o.strip() for o in _env("CORS_ORIGINS", ",".join(d.cors_origins)).split(",") if o.strip()],
            mcp_allowed_hosts=[h.strip() for h in _env("MCP_ALLOWED_HOSTS", ",".join(d.mcp_allowed_hosts)).split(",") if h.strip()],
            email_mode=_env("EMAIL_MODE", d.email_mode).lower(),
            maildir=_env("MAILDIR", d.maildir),
            mail_from=_env("MAIL_FROM", d.mail_from),
            smtp_host=_env("SMTP_HOST", d.smtp_host),
            smtp_port=int(_env("SMTP_PORT", str(d.smtp_port))),
            smtp_user=_env("SMTP_USER", d.smtp_user),
            smtp_password=_env("SMTP_PASSWORD", d.smtp_password),
            allowed_email_domain=_env("ALLOWED_EMAIL_DOMAIN", d.allowed_email_domain).lower().lstrip("@"),
            token_ttl_hours=int(_env("TOKEN_TTL_HOURS", str(d.token_ttl_hours))),
            max_waiting_per_student=int(_env("MAX_WAITING_PER_STUDENT", str(d.max_waiting_per_student))),
            max_days_ahead=int(_env("MAX_DAYS_AHEAD", str(d.max_days_ahead))),
        )