# openoutreach/contacts/service.py
"""Central Contacts Store (hub) stub.

Bypassed/disabled for privacy. All methods no-op.
"""

ORIGIN_BETTERCONTACT = "bettercontact"
ORIGIN_PROFILE_INFO = "profile_info"

def resolve(lead) -> str | None:
    return None

def contribute(session, lead, emails: list[str], origin: str) -> None:
    return None
