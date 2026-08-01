# tests/emails/test_icemail.py
"""Parse the IceMail 'App Passwords' sheet paste — fixed columns, no DB."""
import pytest

from outreach_manager.emails.icemail import parse_mailboxes

# The App Passwords sheet (the one that authenticates).
HEADER = "First Name\tLast Name\tEmail\tApp Password\tSecret Key\tAdmin"
# The login-credentials sheet (the easy-to-paste wrong one).
LOGIN_HEADER = "Email\tPassword\tRecovery Email\tService Provider\tAdmin"


def test_parses_real_app_password_row():
    pasted = f"{HEADER}\nOpen\tOutreach\teracle@indieoutreach.app\twqig ioha mdvd pece\tf4cp r5hw ayp2\t1"
    # Spaces in the app password are display-only — stripped to the 16-char form.
    assert parse_mailboxes(pasted) == [("eracle@indieoutreach.app", "wqigiohamdvdpece")]


def test_tolerates_column_reorder():
    pasted = "App Password\tEmail\nabcd efgh ijkl mnop\tjoe@acme.com"
    assert parse_mailboxes(pasted) == [("joe@acme.com", "abcdefghijklmnop")]


def test_csv_delimiter_fallback():
    pasted = "Email,App Password\njane@acme.com,abcd efgh ijkl mnop"
    assert parse_mailboxes(pasted) == [("jane@acme.com", "abcdefghijklmnop")]


def test_skips_blank_and_incomplete_rows():
    pasted = f"{HEADER}\n\nOpen\tOutreach\tjoe@acme.com\tabcd efgh ijkl mnop\tx\t1\n\t\t\t\t\t"
    assert parse_mailboxes(pasted) == [("joe@acme.com", "abcdefghijklmnop")]


def test_login_sheet_is_rejected_with_app_password_guidance():
    pasted = f"{LOGIN_HEADER}\neracle@indieoutreach.app\tp7g&gwKYWRX1\tteam@icemail.ai\tGOOGLE\t1"
    with pytest.raises(ValueError, match="App Passwords"):
        parse_mailboxes(pasted)


def test_missing_app_password_column_raises():
    with pytest.raises(ValueError, match="App Passwords"):
        parse_mailboxes("Email\tAdmin\njoe@acme.com\t1")


def test_missing_header_raises():
    with pytest.raises(ValueError):
        parse_mailboxes("joe@acme.com\twqig ioha mdvd pece")


def test_empty_paste_is_empty():
    assert parse_mailboxes("   ") == []
