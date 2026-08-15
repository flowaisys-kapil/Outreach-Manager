# openoutreach/core/tz_country.py
"""Resolve an active-hours timezone from a LinkedIn profile's country.

The active-hours window mimics the *operator's* waking rhythm, so the zone we
want is "the local time where this account plausibly logs in from". The
LinkedIn self-profile exposes no timezone — only an ISO-3166-1 alpha-2
``country_code`` — so we map country → a representative IANA zone using the
IANA ``zone.tab`` data shipped by ``pytz`` (``country_timezones``).

Country granularity is deliberate: the window is a coarse 10-hour band, and the
only failure that matters is landing on the wrong *continent* (the old
UTC-everywhere bug put a US/Asia operator "active" while asleep). A multi-zone
country's zones all sit within a few hours of each other, so the first
``zone.tab`` entry is a fine representative — intra-country spread keeps the
window squarely within human hours either way.

Resolution uses stdlib ``zoneinfo`` for the actual window math (pytz is only the
country→zone data table); the slim runtime image already ships the zoneinfo
database, so no extra tz-data dependency is needed.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytz


def timezone_for_country(country_code: str | None) -> str | None:
    """Representative IANA zone for an ISO-3166-1 alpha-2 ``country_code``.

    Returns None for an empty, unknown, or unresolvable code — the caller
    treats None as "no active-hours gating" rather than guessing UTC.
    """
    if not country_code:
        return None
    zones = pytz.country_timezones.get(country_code.strip().lower())
    if not zones:
        return None
    name = zones[0]
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name
