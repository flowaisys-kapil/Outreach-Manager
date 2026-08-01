# outreach_manager/core/daemon.py
"""DEPRECATED DAEMON WRAPPER — Compatibility Entry Point.

NOTE: As of Ticket 2 (Session Executor), the infinite daemon execution loop
has been replaced by the single-session engine in ``outreach_manager.core.session_executor``.

This file is maintained as a thin compatibility entry point. Calling ``run_daemon()``
delegates to ``session_executor.run_session()`` to run a single outreach session and exit cleanly.
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo
from django.utils import timezone

from outreach_manager.core.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ENABLE_ACTIVE_HOURS,
)
from outreach_manager.core.session_executor import run_session, SessionSummary
from outreach_manager.linkedin.scheduler import reconcile

logger = logging.getLogger(__name__)


def seconds_until_active(tz_name: str | None = None) -> float:
    """Seconds until the active-hours window opens (backwards compatibility)."""
    from outreach_manager.core import daemon
    if not daemon.ENABLE_ACTIVE_HOURS or tz_name is None:
        return 0.0
    tz = ZoneInfo(tz_name)
    now_local = timezone.localtime(timezone.now(), tz)
    cur_h = now_local.hour
    if daemon.ACTIVE_START_HOUR <= cur_h < daemon.ACTIVE_END_HOUR:
        return 0.0
    if cur_h >= daemon.ACTIVE_END_HOUR:
        tomorrow_start = (now_local + timezone.timedelta(days=1)).replace(
            hour=daemon.ACTIVE_START_HOUR, minute=0, second=0, microsecond=0,
        )
        return (tomorrow_start - now_local).total_seconds()
    today_start = now_local.replace(
        hour=daemon.ACTIVE_START_HOUR, minute=0, second=0, microsecond=0,
    )
    return (today_start - now_local).total_seconds()


def run_daemon(session, exit_on_empty: bool = False) -> SessionSummary:
    """Compatibility entry point delegating daemon startup to Session Executor."""
    logger.info("run_daemon invoked — delegating to Session Executor (single-session engine)...")
    return run_session(session, exit_on_empty=exit_on_empty)
