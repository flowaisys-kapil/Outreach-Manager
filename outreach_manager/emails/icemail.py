# openoutreach/emails/icemail.py
"""Parse IceMail's 'App Passwords' sheet, pasted from the exported XLS.

The IceMail export is a multi-sheet XLS. The one we need is the **App Passwords**
sheet — ``First Name | Last Name | Email | App Password | Secret Key | Admin`` —
because Google Workspace boxes have 2-step verification on, so SMTP only accepts a
16-char **app password**, not the account login password. We read the ``Email`` and
``App Password`` columns by name (host/port are the Google-box constants on the
Mailbox model) and strip the display spaces Google shows in app passwords.

The other sheet — the login-credentials one (``Email | Password | Recovery Email
| …``) — carries the *login* password, which Gmail rejects for SMTP (534-5.7.9).
Pasting it is the easy mistake, so we detect it and say which sheet to use instead.
"""
from __future__ import annotations

import csv

_WRONG_SHEET = (
    "That looks like the login-credentials tab (it has a 'Password' column). "
    "Gmail rejects the login password for sending — in the same IceMail XLS you "
    "downloaded, switch to the **App Passwords** tab (columns: Email, App Password) "
    "and paste that one instead."
)

_NO_APP_PASSWORD = (
    "Couldn't find an 'App Password' column. In the IceMail XLS you downloaded, "
    "switch to the **App Passwords** tab and paste that sheet, including its header "
    "row (Email, App Password)."
)


def parse_mailboxes(pasted: str) -> list[tuple[str, str]]:
    """Return ``(email, app_password)`` pairs from a pasted App-Passwords block.

    The paste must include the header row. Raises ``ValueError`` with sheet-specific
    guidance when the App Password column is absent — including the common case of
    pasting the login-credentials sheet by mistake.
    """
    lines = [ln for ln in pasted.splitlines() if ln.strip()]
    if not lines:
        return []

    delimiter = "\t" if "\t" in lines[0] else ","
    rows = list(csv.reader(lines, delimiter=delimiter))
    header = [c.strip().lower() for c in rows[0]]

    if "email" not in header:
        raise ValueError(_NO_APP_PASSWORD)
    i_email = header.index("email")

    if "app password" in header:
        i_pw = header.index("app password")
    elif "password" in header:
        raise ValueError(_WRONG_SHEET)
    else:
        raise ValueError(_NO_APP_PASSWORD)

    pairs: list[tuple[str, str]] = []
    for row in rows[1:]:
        if len(row) <= max(i_email, i_pw):
            continue
        email = row[i_email].strip()
        # Google shows app passwords in 4 space-separated groups; the spaces are
        # display-only — strip them to the canonical 16-char form for SMTP auth.
        app_password = row[i_pw].replace(" ", "")
        if email and app_password:
            pairs.append((email, app_password))
    return pairs
