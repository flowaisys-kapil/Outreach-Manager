# openoutreach/core/scheduler.py
"""DEPRECATED PASSIVE WRAPPER — Legacy scheduler primitives and compatibility exports.

DEPRECATION NOTICE:
As of Ticket 1.5 (Complete Scheduler Decoupling), this module contains NO active execution
or scheduling logic. All runtime execution scheduling is owned by ``outreach_manager.linkedin.scheduler``
and all planning primitives reside in ``outreach_manager.linkedin.planning``.

This file is maintained strictly as a passive backward-compatibility layer for existing tests.
No runtime application code in ``openoutreach/`` depends on this module.
"""
from __future__ import annotations

import warnings

from outreach_manager.linkedin.planning import (
    ENABLE_ACTIVE_HOURS,
    ACTIVE_START_HOUR,
    ACTIVE_END_HOUR,
    CHECK_PENDING_DAILY_CAP,
    _working_intervals,
    _has_pending,
    _create_lazy_slots,
    _plan_slots,
    working_seconds_in_window,
    poisson_slot_times,
    plan_connect_window,
    plan_follow_up_window,
    plan_check_pending_window,
    flush_email_queue,
    plan_extraction_window,
    seconds_until_tomorrow,
)
from outreach_manager.linkedin.scheduler import (
    on_deal_state_entered,
    reconcile,
    _recover_stale_running_tasks,
)

warnings.warn(
    "outreach_manager.core.scheduler is deprecated and passive. "
    "Import from outreach_manager.linkedin.scheduler or outreach_manager.linkedin.planning instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "_working_intervals",
    "_has_pending",
    "_create_lazy_slots",
    "_plan_slots",
    "working_seconds_in_window",
    "poisson_slot_times",
    "plan_connect_window",
    "plan_follow_up_window",
    "plan_check_pending_window",
    "flush_email_queue",
    "plan_extraction_window",
    "seconds_until_tomorrow",
    "on_deal_state_entered",
    "reconcile",
    "_recover_stale_running_tasks",
]
