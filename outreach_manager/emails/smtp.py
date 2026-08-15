# openoutreach/emails/smtp.py
"""Auth-only SMTP check, run when a mailbox is imported.

No test send — boxes are mid-warmup; we only confirm the credentials log in.
"""
from __future__ import annotations

import smtplib


def verify_auth(host: str, port: int, username: str, password: str) -> tuple[bool, str]:
    """Connect, STARTTLS, log in, quit. Return ``(ok, message)``.

    A Google/IceMail box rejects its login password with 534/535 — the message
    surfaces the "use the app password" hint for that case.
    """
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(username, password)
        return True, "ok"
    except smtplib.SMTPAuthenticationError as e:
        hint = (
            " — paste the Google app password, not the mailbox login password"
            if e.smtp_code in (534, 535) else ""
        )
        return False, f"auth rejected ({e.smtp_code}){hint}"
    except (smtplib.SMTPException, OSError) as e:
        return False, f"connection failed: {e}"
