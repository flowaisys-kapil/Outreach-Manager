# openoutreach/core/routine_planner.py
"""Routine Planner — Platform-Independent Humanized Execution Time Generator.

Calculates the single next valid outreach execution time based on centralized runtime configuration.
Returns only a single `datetime.datetime` object. Contains zero platform-specific logic.
"""
from __future__ import annotations

import datetime
import logging
import random
from typing import Any

from django.utils import timezone

from outreach_manager.core.config import AppConfig, ConfigurationError, get_config

logger = logging.getLogger(__name__)


def parse_and_validate_windows(windows_raw: Any) -> list[tuple[int, int]]:
    """Parse and validate working windows from config; fail fast on invalid inputs."""
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ConfigurationError("working_windows must be a non-empty list of window dictionaries.")

    parsed: list[tuple[int, int]] = []
    seen = set()

    for w in windows_raw:
        if not isinstance(w, dict) or "start" not in w or "end" not in w:
            raise ConfigurationError(f"Invalid working window format: {w}. Expected dict with 'start' and 'end'.")

        try:
            start = int(w["start"])
            end = int(w["end"])
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(f"Working window hours must be integers in {w}: {exc}") from exc

        if not (0 <= start < end <= 24):
            raise ConfigurationError(
                f"Invalid working window range [{start}, {end}]. Start must be >= 0 and end must be <= 24 with start < end."
            )

        window_tuple = (start, end)
        if window_tuple in seen:
            raise ConfigurationError(f"Duplicate working window detected: [{start}, {end}].")
        seen.add(window_tuple)
        parsed.append(window_tuple)

    # Sort windows chronologically
    parsed.sort(key=lambda x: (x[0], x[1]))

    # Check for overlapping windows
    for i in range(len(parsed) - 1):
        if parsed[i][1] > parsed[i + 1][0]:
            raise ConfigurationError(
                f"Overlapping working windows detected: [{parsed[i][0]}, {parsed[i][1]}] and [{parsed[i + 1][0]}, {parsed[i + 1][1]}]."
            )

    return parsed


def _calculate_candidate_times_for_date(
    target_date: datetime.date,
    windows: list[tuple[int, int]],
    n_sessions: int,
    rnd: random.Random,
) -> list[datetime.datetime]:
    """Helper to compute humanized candidate session start times for a specific date."""
    session_times: list[datetime.datetime] = []
    m_windows = len(windows)
    allocations = [0] * m_windows

    for i in range(n_sessions):
        allocations[i % m_windows] += 1

    local_tz = timezone.get_current_timezone()

    for idx, (win_start_h, win_end_h) in enumerate(windows):
        count = allocations[idx]
        if count == 0:
            continue

        win_duration_minutes = (win_end_h - win_start_h) * 60
        sub_duration_minutes = win_duration_minutes / count

        for sub_idx in range(count):
            sub_start_min = win_start_h * 60 + sub_idx * sub_duration_minutes
            sub_end_min = sub_start_min + sub_duration_minutes

            max_offset = max(1.0, (sub_end_min - sub_start_min) - (5.0 if sub_duration_minutes >= 15 else 0.0))
            offset_minutes = rnd.uniform(0.0, max_offset)

            session_minute_float = sub_start_min + offset_minutes
            hour = int(session_minute_float // 60)
            minute = int(session_minute_float % 60)
            second = rnd.randint(0, 59)

            if hour >= win_end_h:
                hour = win_end_h - 1
                minute = 59

            dt_local = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                hour, minute, second, tzinfo=local_tz,
            )
            session_times.append(dt_local)

    return sorted(list(dict.fromkeys(session_times)))


def calculate_next_execution_time(
    from_time: datetime.datetime | None = None,
    config: AppConfig | None = None,
    seed: Any | None = None,
) -> datetime.datetime:
    """Calculate the single next valid humanized outreach execution time.

    Consumes centralized runtime configuration and returns the single next execution start time.
    Flow:
        Find next eligible active day -> Find next eligible working window -> Generate ONE humanized execution time -> Return datetime.

    Parameters
    ----------
    from_time : datetime.datetime | None
        Current time boundary (defaults to local now).
    config : AppConfig | None
        Runtime configuration instance (defaults to get_config()).
    seed : Any | None
        Random seed for reproducible testing.

    Returns
    -------
    datetime.datetime
        Single next execution time.
    """
    if config is None:
        config = get_config()

    if from_time is None:
        from_time = timezone.localtime()
    elif from_time.tzinfo is None:
        from_time = timezone.make_aware(from_time, timezone.get_current_timezone())

    active_days = [d.strip().lower() for d in config.runtime.active_days]
    windows = parse_and_validate_windows(config.runtime.working_windows)

    n_sessions = config.runtime.sessions_per_day
    if not (1 <= n_sessions <= 5):
        raise ConfigurationError(f"sessions_per_day {n_sessions} must be an integer between 1 and 5.")

    rnd = random.Random(seed) if seed is not None else random.Random()
    current_date = from_time.date()
    max_days_to_search = 14

    for day_offset in range(max_days_to_search):
        candidate_date = current_date + datetime.timedelta(days=day_offset)
        day_name = candidate_date.strftime("%A").lower()

        if day_name not in active_days:
            continue

        candidate_times = _calculate_candidate_times_for_date(
            target_date=candidate_date,
            windows=windows,
            n_sessions=n_sessions,
            rnd=rnd,
        )

        for session_dt in candidate_times:
            if session_dt > from_time:
                logger.info(
                    "[PLANNER] Calculated next execution time: %s (from %s)",
                    session_dt.strftime("%Y-%m-%d %H:%M:%S"), from_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return session_dt

    raise ConfigurationError(
        "Could not calculate a valid next execution time within 14 days. Check active_days and working_windows config."
    )
