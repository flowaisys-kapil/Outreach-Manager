# tests/emails/test_smtp.py
"""Auth-only SMTP check — mock smtplib at the boundary."""
import smtplib
from unittest.mock import patch

from outreach_manager.emails.smtp import verify_auth


def test_auth_ok():
    with patch("smtplib.SMTP") as smtp_cls:
        conn = smtp_cls.return_value.__enter__.return_value
        ok, message = verify_auth("smtp.gmail.com", 587, "u", "p")
    assert ok and message == "ok"
    conn.starttls.assert_called_once()
    conn.login.assert_called_once_with("u", "p")


def test_login_password_rejection_surfaces_app_password_hint():
    error = smtplib.SMTPAuthenticationError(534, b"application-specific password required")
    with patch("smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value.__enter__.return_value.login.side_effect = error
        ok, message = verify_auth("smtp.gmail.com", 587, "u", "p")
    assert not ok and "app password" in message and "534" in message


def test_connection_failure_is_reported_not_raised():
    with patch("smtplib.SMTP", side_effect=OSError("no route to host")):
        ok, message = verify_auth("smtp.gmail.com", 587, "u", "p")
    assert not ok and "connection failed" in message
