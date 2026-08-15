# openoutreach/linkedin/scheduler.py
"""Centralized Execution Scheduler Service.

Replaces continuous eligibility scanning with timestamp-driven scheduling
(``next_action_at``) and atomic worker claiming (``claimed_at``).

Key principles:
1. State changes & task completions trigger ``schedule_next_action()``.
2. Workers use ``claim_due_deal()`` to claim work without race conditions.
3. ``has_due_work()`` allows the daemon to sleep or exit idle without launching browser sessions.
4. Structurally logs task execution details.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from termcolor import colored

from outreach_manager.core.conf import CAMPAIGN_CONFIG
from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.models import Task

logger = logging.getLogger(__name__)

CLAIM_STALE_MINUTES = 30
DEFAULT_RETRY_BACKOFF_HOURS = 1
DEFAULT_NUDGE_INTERVAL_DAYS = 3


def _already_messaged(deal: Deal) -> bool:
    """Return True if this deal has received an initial first message."""
    if deal.first_message_sent_at is not None:
        return True
    from outreach_manager.chat.models import ChatMessage
    return ChatMessage.objects.filter(deal=deal).exists()


def schedule_next_action(
    deal: Deal,
    action_outcome: str | None = None,
    delay_hours: float | None = None,
) -> datetime | None:
    """Determine and stamp ``deal.next_action_at`` based on state & task outcome.

    Rules:
      - TASK FAILURE / EXCEPTION: retry backoff (default 1 hour).
      - TERMINAL (COMPLETED, FAILED, EMAILED, RESPONDED): None.
      - CONNECTED (unmessaged): now (due for FIRST_MESSAGE).
      - CONNECTED (messaged): now + cooldown (unanswered_count * 3d or delay_hours).
      - PENDING: now + backoff_hours.
      - READY_TO_CONNECT / QUALIFIED / READY_TO_EMAIL: now.
    """
    now = timezone.now()
    state = DealState(deal.state) if isinstance(deal.state, str) else deal.state

    # 1. Error / Failure Retry
    if action_outcome in ("error", "failed", "retry"):
        next_time = now + timedelta(hours=DEFAULT_RETRY_BACKOFF_HOURS)
        deal.next_action_at = next_time
        deal.claimed_at = None
        update_fields = ["next_action_at", "claimed_at", "update_date"]
        # For PENDING deals, also push next_check_pending_at so claim_due_deal
        # doesn't re-claim the same deal immediately on the next iteration.
        current_state = DealState(deal.state) if isinstance(deal.state, str) else deal.state
        if current_state == DealState.PENDING:
            deal.next_check_pending_at = next_time
            update_fields.append("next_check_pending_at")
        deal.save(update_fields=update_fields)
        return next_time

    # 2. Terminal or Inactive States
    if state in (
        DealState.COMPLETED,
        DealState.FAILED,
        DealState.EMAILED,
        DealState.RESPONDED,
        DealState.CLOSED_WON,
        DealState.CLOSED_LOST,
    ) or deal.outcome != "":
        deal.next_action_at = None
        deal.claimed_at = None
        deal.save(update_fields=["next_action_at", "claimed_at", "update_date"])
        return None

    # 3. State-specific scheduling
    if state == DealState.CONNECTED:
        update_fields = ["next_action_at", "claimed_at", "update_date"]
        if deal.next_check_pending_at is not None:
            deal.next_check_pending_at = None
            update_fields.append("next_check_pending_at")

        if not _already_messaged(deal):
            # Unmessaged -> FIRST_MESSAGE is due immediately
            next_time = now
        else:
            # Messaged -> FOLLOW_UP scheduling
            if action_outcome == "wait":
                hrs = delay_hours if delay_hours is not None else 72.0
                next_time = now + timedelta(hours=hrs)
            else:
                from outreach_manager.chat.models import ChatMessage
                messages = ChatMessage.objects.filter(deal=deal)
                last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
                nudges = messages.filter(is_outgoing=True)
                if last_reply:
                    nudges = nudges.filter(creation_date__gt=last_reply.creation_date)
                count = max(1, nudges.count())
                days = count * DEFAULT_NUDGE_INTERVAL_DAYS
                next_time = now + timedelta(days=days)
        deal.next_action_at = next_time
        deal.claimed_at = None
        deal.save(update_fields=update_fields)
        return next_time

    elif state == DealState.PENDING:
        backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
        next_time = now + timedelta(hours=backoff)
        deal.next_action_at = next_time
        deal.next_check_pending_at = next_time
        deal.claimed_at = None
        deal.save(update_fields=["next_action_at", "next_check_pending_at", "claimed_at", "update_date"])
        return next_time

    elif state in (DealState.READY_TO_CONNECT, DealState.QUALIFIED, DealState.READY_TO_EMAIL):
        next_time = now
        deal.next_action_at = next_time
        deal.claimed_at = None
        update_fields = ["next_action_at", "claimed_at", "update_date"]
        if deal.next_check_pending_at is not None:
            deal.next_check_pending_at = None
            update_fields.append("next_check_pending_at")
        deal.save(update_fields=update_fields)
        return next_time

    # Default fallback
    deal.claimed_at = None
    update_fields = ["claimed_at", "update_date"]
    if deal.next_check_pending_at is not None:
        deal.next_check_pending_at = None
        update_fields.append("next_check_pending_at")
    deal.save(update_fields=update_fields)
    return deal.next_action_at


def claim_due_deal(campaign, states: list[str | DealState], task_name: str) -> Deal | None:
    """Atomically claim the oldest due Deal for execution in *campaign*.

    Prevents multiple daemon instances or parallel cycles from executing
    the same Deal simultaneously. Stale claims (>30 min) are automatically override-eligible.
    Workflow-aware: filters candidates based on task_name requirements.
    """
    now = timezone.now()
    stale_threshold = now - timedelta(minutes=CLAIM_STALE_MINUTES)
    state_vals = [s.value if hasattr(s, "value") else s for s in states]
    tn = (task_name or "").upper().strip()

    with transaction.atomic():
        # Due filter:
        # PENDING state / CHECK_PENDING task checks next_check_pending_at as well as next_action_at.
        # Non-PENDING states check next_action_at <= now or NULL.
        due_q = Q(next_action_at__isnull=True) | Q(next_action_at__lte=now)
        if DealState.PENDING.value in state_vals or DealState.PENDING in state_vals or tn == "CHECK_PENDING":
            due_q |= Q(next_check_pending_at__isnull=True) | Q(next_check_pending_at__lte=now)

        candidates = (
            Deal.objects.select_for_update(skip_locked=True)
            .filter(
                campaign=campaign,
                state__in=state_vals,
                lead__disqualified=False,
            )
            .filter(due_q)
            .filter(
                Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_threshold)
            )
        )

        # Apply workflow-aware filtering based on task_name
        if tn == "FIRST_MESSAGE":
            # Only return Deals that have NOT received a first message
            candidates = candidates.filter(
                first_message_sent_at__isnull=True
            ).exclude(
                messages__isnull=False
            )
        elif tn == "FOLLOW_UP":
            # Only return Deals that HAVE received a first message
            candidates = candidates.filter(
                Q(first_message_sent_at__isnull=False) | Q(messages__isnull=False)
            ).distinct()
        elif tn == "CHECK_PENDING":
            # Only return Deals pending recheck
            candidates = candidates.filter(
                Q(next_check_pending_at__isnull=True)
                | Q(next_check_pending_at__lte=now)
                | Q(next_action_at__lte=now)
            )
        elif tn in ("REPLY", "REPLY_UNREAD"):
            candidates = candidates.filter(
                messages__is_outgoing=False
            ).distinct()

        candidates = candidates.order_by("next_action_at", "update_date")

        target_deal = candidates.first()
        if target_deal:
            target_deal.claimed_at = now
            target_deal.save(update_fields=["claimed_at", "update_date"])
            return target_deal

    return None


def release_claim(deal: Deal, next_action_at: datetime | None = None) -> None:
    """Clear claimed_at and optionally set next_action_at on a Deal."""
    deal.claimed_at = None
    if next_action_at is not None:
        deal.next_action_at = next_action_at
        deal.save(update_fields=["claimed_at", "next_action_at", "update_date"])
    else:
        deal.save(update_fields=["claimed_at", "update_date"])


def earliest_due_time(campaigns) -> datetime | None:
    """Return the earliest next_action_at across all campaigns, or None if no future work."""
    campaign_ids = [c.pk for c in campaigns] if campaigns else []
    if not campaign_ids:
        return None

    now = timezone.now()
    stale_threshold = now - timedelta(minutes=CLAIM_STALE_MINUTES)

    earliest_deal = (
        Deal.objects.filter(
            campaign_id__in=campaign_ids,
            lead__disqualified=False,
        )
        .filter(Q(next_action_at__isnull=False) | Q(next_check_pending_at__isnull=False))
        .exclude(
            state__in=[
                DealState.COMPLETED.value,
                DealState.FAILED.value,
                DealState.EMAILED.value,
                DealState.RESPONDED.value,
            ]
        )
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_threshold))
        .order_by("next_action_at", "next_check_pending_at")
        .first()
    )

    earliest_task = (
        Task.objects.filter(
            payload__campaign_id__in=campaign_ids,
            status=Task.Status.PENDING,
        )
        .order_by("scheduled_at")
        .first()
    )

    times = []
    if earliest_deal:
        if earliest_deal.next_action_at:
            times.append(earliest_deal.next_action_at)
        if earliest_deal.next_check_pending_at:
            times.append(earliest_deal.next_check_pending_at)
    if earliest_task and earliest_task.scheduled_at:
        times.append(earliest_task.scheduled_at)

    return min(times) if times else None


def has_due_work(campaigns) -> bool:
    """Return True if any campaign has due work right now."""
    campaign_ids = [c.pk for c in campaigns] if campaigns else []
    if not campaign_ids:
        return False

    now = timezone.now()
    stale_threshold = now - timedelta(minutes=CLAIM_STALE_MINUTES)

    # 1. Check for due deals
    due_deal_exists = (
        Deal.objects.filter(
            campaign_id__in=campaign_ids,
            lead__disqualified=False,
        )
        .exclude(
            state__in=[
                DealState.COMPLETED.value,
                DealState.FAILED.value,
                DealState.EMAILED.value,
                DealState.RESPONDED.value,
            ]
        )
        .filter(
            Q(next_action_at__isnull=True)
            | Q(next_action_at__lte=now)
            | (Q(state=DealState.PENDING.value) & Q(next_check_pending_at__lte=now))
        )
        .filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=stale_threshold))
        .exists()
    )
    return True


def log_execution(
    deal: Deal,
    task_name: str,
    outcome_summary: str,
    next_action_name: str,
    scheduled_at: datetime | None,
) -> None:
    """Output structured execution log (Problem 7 requirement)."""
    public_id = deal.lead.public_identifier if deal and deal.lead else "unknown"
    deal_pk = deal.pk if deal else "?"
    sched_str = (
        scheduled_at.strftime("%Y-%m-%d %H:%M UTC") if scheduled_at else "None"
    )

    log_lines = [
        f"Deal {deal_pk} [{public_id}]",
        f"Task: {task_name}",
        f"Outcome: {outcome_summary}",
        f"Next Action: {next_action_name}",
        f"Scheduled: {sched_str}",
    ]

    header = colored(f"--- [EXECUTION LOG] Deal {deal_pk} ---", "cyan", attrs=["bold"])
    body = "\n".join(f"  {line}" for line in log_lines)
    logger.info("\n%s\n%s\n", header, body)


def on_deal_state_entered(deal) -> None:
    """State-transition hook: update next_action_at on state change."""
    schedule_next_action(deal)


def reconcile(session) -> None:
    """No-op startup reconciliation — deterministic sequence generator manages workflow execution."""
    logger.info("[SCHEDULER] Startup reconciliation completed.")


