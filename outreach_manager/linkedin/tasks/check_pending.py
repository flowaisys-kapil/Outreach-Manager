# openoutreach/linkedin/tasks/check_pending.py
"""Check pending task — re-checks all due PENDING deals in the campaign in a batch.

3-way status resolution:
  CONNECTED — broad scrape or targeted check confirms accepted.
  PENDING   — targeted check confirms request still open.
  UNKNOWN   — could not determine; deal state is left untouched.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.crm.models import DealState, Deal
from linkedin_cli.exceptions import SkipProfile
from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)


def _next_due_pending_deal(campaign):
    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.PENDING,
            next_check_pending_at__lte=timezone.now(),
        )
        .select_related("lead", "campaign")
        .order_by("next_check_pending_at")
        .first()
    )


def _double_backoff(deal) -> float:
    from outreach_manager.core.conf import CAMPAIGN_CONFIG
    current = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
    deal.backoff_hours = current * 2
    deal.save(update_fields=["backoff_hours"])
    return deal.backoff_hours


def _resolve_status_individually(session, deal) -> str:
    """Targeted individual status check for one deal.

    Returns one of three canonical strings:
      "CONNECTED" — confirmed accepted.
      "PENDING"   — confirmed still pending (request still open in UI).
      "UNKNOWN"   — could not determine (profile inaccessible, network error, etc.).
    """
    from linkedin_cli.actions.status import get_connection_status
    from linkedin_cli.enums import ProfileState

    try:
        status = get_connection_status(session, deal.lead.to_profile_dict())
        if status == ProfileState.CONNECTED or str(status) == "CONNECTED":
            return "CONNECTED"
        if status == ProfileState.PENDING or str(status) == "PENDING":
            return "PENDING"
        return "UNKNOWN"
    except Exception as exc:
        logger.warning("Individual status check failed for %s: %s", deal.lead.public_identifier, exc)
        return "UNKNOWN"


def handle_check_pending(task, session, qualifiers) -> WorkflowResult:
    from outreach_manager.linkedin.pipeline.acceptances import (
        check_acceptances_page,
        run_withdrawals_check,
        sync_sent_invitations,
    )
    from outreach_manager.linkedin.scheduler import claim_due_deal, log_execution, schedule_next_action

    campaign = session.campaign
    processed_count = 0
    skipped_count = 0
    errors_count = 0
    accepted_count = 0
    errors_list: list[str] = []

    # Phase A – Synchronization: Make LinkedIn Sent Invitations source of truth
    try:
        sent_requests = sync_sent_invitations(session, campaign)
    except Exception as exc:
        logger.warning("[%s] Phase A LinkedIn invitation sync failed: %s", campaign, exc)
        sent_requests = None

    # Phase B – Processing & Withdrawals

    # B1: Run withdrawals for stale invitations (> 7 days)
    try:
        run_withdrawals_check(session, campaign, sent_requests=sent_requests)
    except Exception as exc:
        logger.warning("[%s] Withdrawal check failed: %s", campaign, exc)

    # Discovery Log
    due_deals_count = Deal.objects.filter(
        campaign=campaign,
        state=DealState.PENDING,
        next_check_pending_at__lte=timezone.now(),
    ).count()
    logger.info("[%s] Check Pending Workflow — Candidates discovered: %d", campaign, due_deals_count)

    connections = None

    # B2: Process due PENDING deals
    pending_deals = list(
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.PENDING,
            lead__disqualified=False,
        ).select_related("lead", "campaign").order_by("next_check_pending_at")
    )

    for deal in pending_deals:


        if connections is None:
            try:
                connections = check_acceptances_page(session)
            except Exception as exc:
                logger.warning("[%s] check_acceptances_page failed: %s", campaign, exc)
                connections = set()

        public_id = deal.lead.public_identifier
        logger.info("[%s] Processing candidate: %s", campaign, public_id)

        try:
            # Action: Determine connection acceptance
            if public_id in connections:
                logger.info("[%s] Action executed: Confirmed accepted via broad scrape", campaign)
                set_profile_state(session, public_id, DealState.CONNECTED.value)
                next_t = schedule_next_action(deal)
                log_execution(deal, "CHECK_PENDING", "Accepted (Broad Scrape)", "FIRST_MESSAGE", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                accepted_count += 1
                processed_count += 1
            else:
                targeted = _resolve_status_individually(session, deal)
                logger.info("[%s] Action executed: Resolved status to %s", campaign, targeted)

                if targeted == "CONNECTED":
                    set_profile_state(session, public_id, DealState.CONNECTED.value)
                    next_t = schedule_next_action(deal)
                    log_execution(deal, "CHECK_PENDING", "Accepted (Targeted)", "FIRST_MESSAGE", next_t)
                    logger.info("[%s] State synchronized for: %s", campaign, public_id)
                    accepted_count += 1
                    processed_count += 1
                elif targeted == "PENDING":
                    old = deal.backoff_hours or 0
                    new = _double_backoff(deal)
                    deal.refresh_from_db()
                    set_profile_state(session, public_id, DealState.PENDING.value)
                    next_t = schedule_next_action(deal)
                    log_execution(deal, "CHECK_PENDING", f"Still Pending (Backoff {new}h)", "CHECK_PENDING", next_t)
                    logger.info("[%s] State synchronized for: %s", campaign, public_id)
                    processed_count += 1
                else:
                    next_t = schedule_next_action(deal, "retry")
                    log_execution(deal, "CHECK_PENDING", "Status Unknown (Inconclusive)", "RETRY_CHECK_PENDING", next_t)
                    logger.info("[%s] State synchronized for: %s", campaign, public_id)
                    skipped_count += 1
        except Exception as exc:
            logger.exception("check_pending error for %s: %s", public_id, exc)
            errors_count += 1
            errors_list.append(f"check_pending error for {public_id}: {exc}")

    logger.info(
        "[%s] Check Pending Workflow Complete — Processed: %d, Skipped: %d, Errors: %d",
        campaign, processed_count, skipped_count, errors_count
    )

    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        errors=errors_list,
        metrics={
            "pending_requests_checked": processed_count + skipped_count,
            "accepted_connections": accepted_count,
        }
    )
