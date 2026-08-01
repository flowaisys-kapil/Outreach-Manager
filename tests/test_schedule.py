from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from outreach_manager.core.daemon import seconds_until_active


def _mock_now(year, month, day, hour, minute=0, tz="UTC"):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


class TestSecondsUntilActive:
    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_inside_active_window(self):
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 12)):
            assert seconds_until_active("UTC") == 0.0

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_before_start(self):
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 7)):
            result = seconds_until_active("UTC")
            assert result == pytest.approx(2 * 3600, abs=1)

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_after_end(self):
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 18)):
            result = seconds_until_active("UTC")
            assert result == pytest.approx(15 * 3600, abs=1)  # 15h to Thu 9am

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_saturday_is_active(self):
        # Sat Mar 21 2026 noon — weekends are no longer skipped.
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 21, 12)):
            assert seconds_until_active("UTC") == 0.0

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_friday_evening_to_saturday_morning(self):
        # Friday 18:00 → Saturday 9:00 = 15h (no weekend skip).
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 20, 18)):
            assert seconds_until_active("UTC") == pytest.approx(15 * 3600, abs=1)

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_timezone_respected(self):
        # Wed 8am Berlin = still before 9am start
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 8, tz="Europe/Berlin")):
            result = seconds_until_active("Europe/Berlin")
            assert result == pytest.approx(3600, abs=1)

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_at_exact_start(self):
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 9)):
            assert seconds_until_active("UTC") == 0.0

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_at_exact_end(self):
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 17)):
            result = seconds_until_active("UTC")
            # Should be outside (end is exclusive), next day 9am = 16h
            assert result == pytest.approx(16 * 3600, abs=1)

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", True)
    @patch("outreach_manager.core.daemon.ACTIVE_START_HOUR", 9)
    @patch("outreach_manager.core.daemon.ACTIVE_END_HOUR", 17)
    def test_no_timezone_never_gates(self):
        # Unresolved timezone (unknown profile country, no override) → never
        # gate, even at an hour that would otherwise be outside the window.
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 18, 3)):
            assert seconds_until_active(None) == 0.0

    @patch("outreach_manager.core.daemon.ENABLE_ACTIVE_HOURS", False)
    def test_disabled_always_active(self):
        with patch("outreach_manager.core.daemon.timezone.localtime", return_value=_mock_now(2026, 3, 21, 23)):
            assert seconds_until_active("UTC") == 0.0
