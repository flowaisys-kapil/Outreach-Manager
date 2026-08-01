# outreach_manager/linkedin/tasks/check_pending.py
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
from outreach_manager.crm.models import DealState
from linkedin_cli.exceptions import SkipProfile

logger = logging.getLogger(__name__)


def _next_due_pending_deal(campaign):
    from outreach_manager.crm.models import Deal

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


def handle_check_pending(task, session, qualifiers):
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

    # B2: Process due PENDING deals
    connections = None

    while True:
        deal = claim_due_deal(campaign, [DealState.PENDING], "CHECK_PENDING")
        if deal is None:
            break

        if connections is None:
            try:
                connections = check_acceptances_page(session)
            except Exception as exc:
                logger.warning("[%s] check_acceptances_page failed: %s", campaign, exc)
                connections = set()

        public_id = deal.lead.public_identifier
        logger.info(
            "[%s] %s %s",
            campaign, colored("▶ check_pending", "magenta", attrs=["bold"]), public_id,
        )

        try:
            if public_id in connections:
                logger.info("[%s] check_pending: %s has accepted! Promoting to CONNECTED.", campaign, public_id)
                set_profile_state(session, public_id, DealState.CONNECTED.value)
                next_t = schedule_next_action(deal)
                log_execution(deal, "CHECK_PENDING", "Accepted (Broad Scrape)", "FIRST_MESSAGE", next_t)
                processed_count += 1
            else:
                targeted = _resolve_status_individually(session, deal)

                if targeted == "CONNECTED":
                    logger.info("[%s] check_pending: %s confirmed CONNECTED via targeted check.", campaign, public_id)
                    set_profile_state(session, public_id, DealState.CONNECTED.value)
                    next_t = schedule_next_action(deal)
                    log_execution(deal, "CHECK_PENDING", "Accepted (Targeted)", "FIRST_MESSAGE", next_t)
                    processed_count += 1
                elif targeted == "PENDING":
                    old = deal.backoff_hours or 0
                    new = _double_backoff(deal)
                    logger.info("%s still pending — backoff %.1fh → %.1fh", public_id, old, new)
                    deal.refresh_from_db()
                    set_profile_state(session, public_id, DealState.PENDING.value)
                    next_t = schedule_next_action(deal)
                    log_execution(deal, "CHECK_PENDING", f"Still Pending (Backoff {new}h)", "CHECK_PENDING", next_t)
                    processed_count += 1
                else:
                    logger.info(
                        "[%s] check_pending: %s status UNKNOWN — leaving deal PENDING for next check.",
                        campaign, public_id,
                    )
                    next_t = schedule_next_action(deal, "retry")
                    log_execution(deal, "CHECK_PENDING", "Status Unknown (Inconclusive)", "RETRY_CHECK_PENDING", next_t)
                    skipped_count += 1
        except Exception as exc:
            logger.exception("check_pending error for %s: %s", public_id, exc)
            errors_count += 1
            errors_list.append(f"check_pending error for {public_id}: {exc}")

    logger.info(
        "[%s] Check Pending Workflow — Processed: %d, Skipped: %d, Errors: %d",
        campaign, processed_count, skipped_count, errors_count
    )

    from outreach_manager.core.workflow_result import WorkflowResult
    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        errors=errors_list,
    )
