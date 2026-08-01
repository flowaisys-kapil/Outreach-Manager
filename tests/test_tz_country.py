from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from outreach_manager.core.tz_country import timezone_for_country


class TestTimezoneForCountry:
    @pytest.mark.parametrize(
        "cc, expected",
        [
            ("it", "Europe/Rome"),
            ("US", "America/New_York"),   # case-insensitive
            (" de ", "Europe/Berlin"),    # whitespace-tolerant
            ("jp", "Asia/Tokyo"),
        ],
    )
    def test_known_country_resolves(self, cc, expected):
        assert timezone_for_country(cc) == expected

    def test_result_is_a_valid_iana_zone(self):
        # The whole point: the returned name resolves in the installed tzdb.
        ZoneInfo(timezone_for_country("au"))

    @pytest.mark.parametrize("cc", [None, "", "  ", "xx", "zz"])
    def test_unknown_or_empty_returns_none(self, cc):
        # None means "no active-hours gating" — never a UTC guess.
        assert timezone_for_country(cc) is None
