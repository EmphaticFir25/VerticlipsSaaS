"""Sends transactional email over SMTP. With no SMTP_HOST it writes to DATA_DIR/outbox.jsonl instead (dev and tests)."""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

from . import config, status


def configured() -> bool:
    return bool(config.SMTP_HOST)


def send(to: str, subject: str, body: str) -> None:
    try:
        _send(to, subject, body)
    except Exception:
        if configured():
            status.record("email", False)
        raise
    if configured():
        status.record("email", True)


def _send(to: str, subject: str, body: str) -> None:
    if not configured():
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = config.DATA_DIR / "outbox.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"to": to, "subject": subject, "body": body, "at": time.time()}) + "\n")
        print(f"[mail] SMTP is not configured. Wrote '{subject}' for {to} to the outbox.")
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.MAIL_FROM, to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        server.starttls(context=ctx)
    with server:
        if config.SMTP_USER:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)


def finished_email(title: str, link: str, ok: bool, message: str = "") -> tuple[str, str]:
    if ok:
        return ("Your clips are ready", f"Your clips for \"{title}\" are ready to review:\n{link}\n\n"
                f"You can turn these emails off in Settings.")
    return ("We couldn't finish your project", f"We couldn't finish \"{title}\".\n{message}\n\nDetails and a retry button are here:\n{link}\n\n"
            "Your minutes were returned. You can turn these emails off in Settings.")


def verification_email(link: str) -> tuple[str, str]:
    return ("Confirm your email for Verticlip",
            f"Welcome to Verticlip.\n\nConfirm your email to start creating clips:\n{link}\n\n"
            "This link works for 3 days. If you didn't sign up, you can ignore this email.")


def reset_email(link: str) -> tuple[str, str]:
    return ("Reset your Verticlip password",
            f"Someone asked to reset the password for this account.\n\nChoose a new password:\n{link}\n\n"
            "This link works for 1 hour and can be used once. If it wasn't you, you can ignore this email.")
