"""Sending email, behind one small interface.

    EMAIL_MODE=file   (default) write each email as a .eml file in MAILDIR.
                      Open them in Outlook or any mail app to see exactly what
                      would have gone out.
    EMAIL_MODE=smtp   really send through an SMTP server, e.g. Gmail with an
                      app password (SMTP_USER, SMTP_PASSWORD, MAIL_FROM).
    EMAIL_MODE=graph  Microsoft Graph, once college IT approves Mail.Send.
                      Not written yet: it fails loudly instead of pretending.

Only `real` mailers need a known faculty address. In file mode a missing
address is written as a placeholder so the flow can be tested end to end.
"""
from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .config import Settings

log = logging.getLogger("mailer")


class MailError(Exception):
    """Sending failed. The message is safe to show to users."""


@dataclass
class Mail:
    to: str
    subject: str
    html: str
    text: str
    reply_to: str | None = None


def _build(mail: Mail, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    return msg


class FileMailer:
    real = False

    def __init__(self, maildir: str, sender: str):
        self.dir = Path(maildir)
        self.sender = sender
        self.sent: list[Path] = []

    def send(self, mail: Mail) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", mail.subject.lower())[:50].strip("-")
        path = self.dir / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{slug}.eml"
        path.write_bytes(bytes(_build(mail, self.sender)))
        self.sent.append(path)
        log.info("email to %s written to %s", mail.to, path)
        return str(path)


class SmtpMailer:
    real = True

    def __init__(self, s: Settings):
        if not (s.smtp_user and s.smtp_password):
            raise RuntimeError("EMAIL_MODE=smtp needs SMTP_USER and SMTP_PASSWORD")
        self.s = s

    def send(self, mail: Mail) -> str:
        msg = _build(mail, self.s.mail_from)
        try:
            with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.s.smtp_user, self.s.smtp_password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.exception("SMTP send to %s failed", mail.to)
            raise MailError("Couldn't send the email right now. Try again in a minute.") from exc
        return msg["Message-ID"]


class GraphMailer:
    real = True

    def send(self, mail: Mail) -> str:
        raise MailError(
            "Outlook sending isn't set up yet (it needs college IT approval). "
            "Use EMAIL_MODE=file or smtp for now."
        )


def make_mailer(s: Settings):
    if s.email_mode == "smtp":
        return SmtpMailer(s)
    if s.email_mode == "graph":
        return GraphMailer()
    return FileMailer(s.maildir, s.mail_from)
