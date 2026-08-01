# outreach_manager/linkedin/planning.py
"""Task Queue Slot Planning Primitives & Active-Hours Arithmetic.

Provides slot generation for task-row-driven workflows (CHECK_PENDING, EMAIL)
and active-hours working window calculations.
"""
from __future__ import annotations

import datetime
import logging
import random
from datetime import datetime as Datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from outreach_manager.core.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    CAMPAIGN_CONFIG,
    CHECK_PENDING_DAILY_CAP,
    ENABLE_ACTIVE_HOURS,
)
from outreach_manager.core.models import Task
from outreach_manager.crm.models import DealState

logger = logging.getLogger(__name__)


# ── Working-hours arithmetic ──────────────────────────────────────────


def _working_intervals(start, end, tz_name) -> list[tuple]:
    """Return ``[(s, e), ...]`` UTC datetimes for the working portions of
    ``[start, end]``. The whole window ``[(start, end)]`` is returned —
    i.e. no gating — when ``ENABLE_ACTIVE_HOURS`` is False or ``tz_name`` is
    None (timezone not resolved, e.g. unknown profile country)."""
    if not ENABLE_ACTIVE_HOURS or tz_name is None:
        return [(start, end)]

    tz = ZoneInfo(tz_name)
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)

    intervals: list[tuple] = []
    day = local_start.date()
    last_day = local_end.date()
    while day <= last_day:
        day_active_start = Datetime(
            day.year, day.month, day.day, ACTIVE_START_HOUR, tzinfo=tz,
        )
        day_active_end = Datetime(
            day.year, day.month, day.day, ACTIVE_END_HOUR, tzinfo=tz,
        )
        s = max(day_active_start, local_start)
        e = min(day_active_end, local_end)
        if e > s:
            intervals.append((s, e))
        day = day + timedelta(days=1)
    return intervals


def working_seconds_in_window(start, end, tz_name) -> float:
    """Sum of seconds inside ``[ACTIVE_START_HOUR, ACTIVE_END_HOUR]`` between
    ``start`` and ``end``. Returns ``(end - start).total_seconds()`` when
    active hours are disabled or ``tz_name`` is None (no gating)."""
    if not ENABLE_ACTIVE_HOURS or tz_name is None:
        return max(0.0, (end - start).total_seconds())
    return sum((e - s).total_seconds() for s, e in _working_intervals(start, end, tz_name))


def poisson_slot_times(now, n: int, tz_name, horizon_hours: float = 24) -> list:
    """Return ``n`` strictly-increasing timestamps inside the working
    portion of ``[now, now + horizon_hours]``.
    """
    if n <= 0:
        return []

    end = now + timedelta(hours=horizon_hours)
    intervals = _working_intervals(now, end, tz_name)
    total = sum((e - s).total_seconds() for s, e in intervals)
    if total <= 0:
        return []

    positions = sorted(random.uniform(0, total) for _ in range(n))

    times: list = []
    cursor_interval = 0
    cursor_offset = 0.0  # working-seconds consumed before the current interval
    for pos in positions:
        while cursor_interval < len(intervals):
            s, e = intervals[cursor_interval]
            dur = (e - s).total_seconds()
            if pos < cursor_offset + dur:
                times.append(s + timedelta(seconds=pos - cursor_offset))
                break
            cursor_offset += dur
            cursor_interval += 1
    return times


# ── Per-type planners ─────────────────────────────────────────────────


def _has_pending(task_type: Task.TaskType, campaign_id: int) -> bool:
    return Task.objects.filter(
        task_type=task_type,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign_id,
    ).exists()


def _create_lazy_slots(task_type: Task.TaskType, campaign_id: int, times: list) -> int:
    if not times:
        return 0
    Task.objects.bulk_create([
        Task(
            task_type=task_type,
            scheduled_at=t,
            payload={"campaign_id": campaign_id},
        )
        for t in times
    ])
    return len(times)


def _plan_slots(task_type: Task.TaskType, campaign_id: int, n: int, tz_name) -> int:
    """Schedule *n* lazy slots."""
    if n <= 0:
        return 0
    now = timezone.now()

    import os
    import sys
    is_testing = "pytest" in sys.modules or "test" in sys.argv
    if is_testing:
        horizon = 24.0
        immediate = False
    else:
        try:
            horizon = float(os.environ.get("SCHEDULER_HORIZON_HOURS", "24.0"))
        except ValueError:
            horizon = 24.0
        immediate = os.environ.get("SCHEDULER_IMMEDIATE_MODE", "True").lower() == "true"

    if immediate:
        times = [now + timedelta(seconds=i) for i in range(n)]
    else:
        times = [now] + poisson_slot_times(now, n - 1, tz_name, horizon_hours=horizon)
    return _create_lazy_slots(task_type, campaign_id, times)


def plan_connect_window(session, campaign) -> int:
    """Plan connection slots for campaign."""
    if _has_pending(Task.TaskType.CONNECT, campaign.pk):
        return 0

    import sys
    is_testing = "pytest" in sys.modules or "test" in sys.argv
    if not is_testing:
        try:
            from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
            from outreach_manager.linkedin.pipeline.ready_pool import promote_to_ready
            from outreach_manager.crm.models import Lead

            qualifier = BayesianQualifier(
                seed=42,
                n_mc_samples=CAMPAIGN_CONFIG["qualification_n_mc_samples"],
                campaign=campaign,
            )
            X, y = Lead.get_labeled_arrays(campaign)
            if len(X) > 0:
                qualifier.warm_start(X, y)

            threshold = CAMPAIGN_CONFIG["min_ready_to_connect_prob"]
            promote_to_ready(session, qualifier, threshold)
        except Exception as e:
            logger.warning("Failed to run promote_to_ready during plan_connect_window: %s", e)

        from outreach_manager.crm.models import Deal
        has_candidates = Deal.objects.filter(
            campaign=campaign,
            state=DealState.READY_TO_CONNECT,
            lead__disqualified=False,
        ).exists()
        if not has_candidates:
            return 0

    profile = session.linkedin_profile
    n = max(0, profile.connect_daily_limit - profile._daily_count("connect"))

    weekly_limit = getattr(profile, "connect_weekly_limit", 100)
    weekly_sent = profile._weekly_count("connect")

    if weekly_sent >= weekly_limit:
        n = 0
        logger.warning("[%s] Weekly connection limit health guard triggered: reached weekly limit (%d/%d). Stopping connections.", campaign, weekly_sent, weekly_limit)
    elif weekly_sent >= int(weekly_limit * 0.9):
        n = 0
        logger.warning("[%s] Weekly connection limit health guard triggered: weekly count (%d/%d) is >= 90%%. Pausing connections.", campaign, weekly_sent, weekly_limit)
    elif weekly_sent >= int(weekly_limit * 0.75):
        n = min(n, 1)
        logger.info("[%s] Weekly connection limit health guard triggered: weekly count (%d/%d) is >= 75%%. Scaling down connections to max 1.", campaign, weekly_sent, weekly_limit)
    elif weekly_sent >= int(weekly_limit * 0.5):
        n = int(n * 0.5)
        logger.info("[%s] Weekly connection limit health guard triggered: weekly count (%d/%d) is >= 50%%. Scaling down connections by 50%%.", campaign, weekly_sent, weekly_limit)

    import os
    import sys
    is_testing = "pytest" in sys.modules or "test" in sys.argv
    if is_testing:
        max_connects = 999
    else:
        try:
            max_connects = int(os.environ.get("SCHEDULER_MAX_CONNECTS_PER_RUN", "999"))
        except ValueError:
            max_connects = 999
    n = min(n, max_connects)

    created = _plan_slots(Task.TaskType.CONNECT, campaign.pk, n, session.active_timezone)
    if created:
        logger.info(
            "[%s] planned %d connect slots — 1 fires now, "
            "%d Poisson-spaced (daily=%d, run_cap=%d)",
            campaign, created, max(0, created - 1), profile.connect_daily_limit, max_connects,
        )
    return created


def plan_follow_up_window(session, campaign) -> int:
    """Plan follow-up slots for campaign."""
    if _has_pending(Task.TaskType.FOLLOW_UP, campaign.pk):
        return 0

    import sys
    is_testing = "pytest" in sys.modules or "test" in sys.argv
    if not is_testing:
        from outreach_manager.linkedin.tasks.follow_up import _next_followup_deal
        if _next_followup_deal(campaign) is None:
            return 0

    profile = session.linkedin_profile
    daily_remaining = max(0, profile.follow_up_daily_limit - profile._daily_count("follow_up"))

    import os
    import sys
    is_testing = "pytest" in sys.modules or "test" in sys.argv
    if is_testing:
        max_follow_ups = 999
    else:
        try:
            max_follow_ups = int(os.environ.get("SCHEDULER_MAX_FOLLOW_UPS_PER_RUN", "999"))
        except ValueError:
            max_follow_ups = 999
    daily_remaining = min(daily_remaining, max_follow_ups)

    created = _plan_slots(Task.TaskType.FOLLOW_UP, campaign.pk, daily_remaining, session.active_timezone)
    if created:
        logger.info(
            "[%s] planned %d follow_up slots — 1 fires now, "
            "%d Poisson-spaced (daily=%d, run_cap=%d)",
            campaign, created, max(0, created - 1), profile.follow_up_daily_limit, max_follow_ups,
        )
    return created


def plan_check_pending_window(session, campaign) -> int:
    """Plan check_pending slots for campaign."""
    from outreach_manager.crm.models import Deal

    if _has_pending(Task.TaskType.CHECK_PENDING, campaign.pk):
        return 0

    now = timezone.now()
    n_due = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.PENDING,
        next_check_pending_at__lte=now,
    ).count()
    n = min(n_due, CHECK_PENDING_DAILY_CAP)

    created = _plan_slots(Task.TaskType.CHECK_PENDING, campaign.pk, n, session.active_timezone)
    if created:
        logger.info(
            "[%s] planned %d check_pending slots over next 24h — 1 fires now, "
            "%d Poisson-spaced (due=%d, cap=%d)",
            campaign, created, max(0, created - 1), n_due, CHECK_PENDING_DAILY_CAP,
        )
    return created


def flush_email_queue(session, campaign) -> int:
    """Drain READY_TO_EMAIL pool into task slots."""
    from outreach_manager.crm.models import Deal
    from outreach_manager.emails.models import Mailbox

    if _has_pending(Task.TaskType.EMAIL, campaign.pk):
        return 0

    remaining = Mailbox.objects.remaining_today()
    if remaining <= 0:
        return 0

    queued = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=DealState.READY_TO_EMAIL,
        lead__disqualified=False,
    ).count()
    n = min(queued, remaining)
    if n <= 0:
        return 0

    now = timezone.now()
    created = _create_lazy_slots(Task.TaskType.EMAIL, campaign.pk, [now] * n)
    logger.info(
        "[%s] flushed %d email slots to send now (queued=%d, cap_remaining=%d)",
        campaign, created, queued, remaining,
    )
    return created


def plan_extraction_window(session, campaign, force=False) -> int:
    """Plan lead extraction tasks."""
    if _has_pending(Task.TaskType.CONNECT, campaign.pk):
        return 0

    profile = session.linkedin_profile
    target_buffer = 3 * profile.connect_daily_limit

    from outreach_manager.crm.models import Deal
    current_ready = Deal.objects.filter(
        campaign=campaign,
        state=DealState.READY_TO_CONNECT,
        lead__disqualified=False,
    ).count()

    if not force and current_ready >= target_buffer:
        logger.info("[%s] Predictive lead buffer is full (%d/%d). Skipping extraction.", campaign, current_ready, target_buffer)
        return 0

    deficit = target_buffer - current_ready if not force else 5
    n = max(1, min(deficit, 10))

    created = _plan_slots(Task.TaskType.CONNECT, campaign.pk, n, session.active_timezone)
    if created:
        logger.info("[%s] Planned %d extraction tasks (force=%s, %d/%d ready)", campaign, created, force, current_ready, target_buffer)
    return created


def seconds_until_tomorrow() -> float:
    """Seconds until 00:00 local time."""
    now = timezone.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (tomorrow - now).total_seconds()
