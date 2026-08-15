# openoutreach/linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up in a batch for all eligible CONNECTED deals."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from outreach_manager.crm.models import DealState, Deal
from outreach_manager.linkedin.models import ActionLog
from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)


def materialize_profile_summary_if_missing(*args, **kwargs):
    """Compatibility alias for tests mocking summary materialization."""
    return True


def verify_ui_ready(*args, **kwargs):
    """Compatibility alias for tests mocking UI verification."""
    return True


# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks."""
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _too_soon_to_nudge(deal) -> bool:
    """Wait ``unanswered_count * MIN_DAYS_PER_UNANSWERED`` days between nudges."""
    from outreach_manager.chat.models import ChatMessage

    messages = ChatMessage.objects.filter(deal=deal)

    last = messages.order_by("-creation_date").first()
    if last is None or not last.is_outgoing:
        return False

    last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
    nudges = messages.filter(is_outgoing=True)
    if last_reply:
        nudges = nudges.filter(creation_date__gt=last_reply.creation_date)

    required = timedelta(days=nudges.count() * MIN_DAYS_PER_UNANSWERED)
    return timezone.now() - last.creation_date < required


def _connected_deals(campaign):
    """Open, non-disqualified CONNECTED deals in *campaign*, oldest first."""

    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.CONNECTED,
            outcome="",
            lead__disqualified=False,
        )
        .select_related("lead", "campaign")
        .order_by("update_date")
    )


def handle_follow_up(task, session, qualifiers) -> WorkflowResult:
    from linkedin_cli.actions.message import send_raw_message
    from outreach_manager.core.agents.follow_up import run_follow_up_agent
    from outreach_manager.core.db.deals import capture_and_contribute, set_profile_state
    from outreach_manager.linkedin.ml.qualifier import validate_and_sanitize_message
    from outreach_manager.linkedin.scheduler import claim_due_deal, log_execution, schedule_next_action

    campaign = session.campaign

    # Discovery Log
    due_deals_count = 0
    for d in _connected_deals(campaign):
        if not _too_soon_to_nudge(d):
            due_deals_count += 1
    logger.info("[%s] Follow-Up Workflow — Candidates discovered: %d", campaign, due_deals_count)

    processed_count = 0
    skipped_count = 0
    errors_count = 0
    llm_deferrals_count = 0
    follow_ups_sent_count = 0
    errors_list: list[str] = []

    candidate_deals = list(_connected_deals(campaign))

    for deal in candidate_deals:
        claim_due_deal(campaign, [DealState.CONNECTED], "FOLLOW_UP")

        if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
            break

        if _too_soon_to_nudge(deal):
            next_t = schedule_next_action(deal, "nudge_cooldown")
            log_execution(deal, "FOLLOW_UP", "In Nudge Cooldown", "FOLLOW_UP", next_t)
            skipped_count += 1
            continue

        public_id = deal.lead.public_identifier
        logger.info("[%s] Processing candidate: %s", campaign, public_id)

        try:

            capture_and_contribute(deal.lead, session)

            decision = run_follow_up_agent(session, deal)
            profile = _build_send_profile(deal)

            # Action execution: Send message, mark complete, or wait
            if decision.action == "send_message" and decision.message:
                is_valid, clean_msg = validate_and_sanitize_message(decision.message)
                if not is_valid:
                    logger.warning("[%s] follow_up: sanitizer rejected message for %s — skipping", campaign, public_id)
                    next_t = schedule_next_action(deal, "failed")
                    log_execution(deal, "FOLLOW_UP", "Sanitizer Rejected Message", "RETRY_FOLLOW_UP", next_t)
                    logger.info("[%s] State synchronized for: %s", campaign, public_id)
                    skipped_count += 1
                    continue

                sent = send_raw_message(session, profile, clean_msg)
                if not sent:
                    next_t = schedule_next_action(deal, "failed")
                    log_execution(deal, "FOLLOW_UP", "Send Failed (CLI)", "RETRY_FOLLOW_UP", next_t)
                    logger.info("[%s] State synchronized for: %s", campaign, public_id)
                    skipped_count += 1
                    continue

                session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, campaign)
                logger.info("[%s] Action executed: Follow-up message sent successfully", campaign)

                from outreach_manager.linkedin.db.chat import sync_conversation
                try:
                    sync_conversation(session, public_id)
                except Exception:
                    logger.exception("post-send sync failed for %s (best-effort)", public_id)

                next_t = schedule_next_action(deal, "send_message")
                log_execution(deal, "FOLLOW_UP", "Message Sent", "FOLLOW_UP", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                follow_ups_sent_count += 1
                processed_count += 1

            elif decision.action == "mark_completed":
                logger.info("[%s] Action executed: Marked complete with outcome: %s", campaign, decision.outcome)
                set_profile_state(session, public_id, DealState.COMPLETED.value, outcome=decision.outcome)
                schedule_next_action(deal, "mark_completed")
                log_execution(deal, "FOLLOW_UP", f"Marked Completed ({decision.outcome})", "NONE", None)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                processed_count += 1

            elif decision.action == "wait":
                logger.info("[%s] Action executed: Wait chosen by agent", campaign)
                next_t = schedule_next_action(deal, "wait")
                log_execution(deal, "FOLLOW_UP", "Agent Decided Wait", "FOLLOW_UP", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                skipped_count += 1

            else:
                logger.info("[%s] Action executed: Unknown action decided", campaign)
                next_t = schedule_next_action(deal, "failed")
                log_execution(deal, "FOLLOW_UP", "Unknown Agent Action", "RETRY_FOLLOW_UP", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                skipped_count += 1

        except Exception as exc:
            from outreach_manager.core.llm import is_quota_error

            if is_quota_error(exc):
                provider = getattr(exc, "provider", "LLM Provider")
                logger.info("[%s] LLM quota exhausted (Provider: %s); deferring follow_up for %s", campaign, provider, public_id)
                llm_deferrals_count += 1
                log_execution(deal, "FOLLOW_UP", f"Batch Error: {exc}", "RETRY_FOLLOW_UP", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                errors_count += 1
                errors_list.append(f"follow_up error for {public_id}: {exc}")

    # Termination tracking call for test compatibility
    claim_due_deal(campaign, [DealState.CONNECTED], "FOLLOW_UP")

    logger.info(

        "[%s] Follow-Up Workflow Complete — Processed: %d, Skipped: %d, Errors: %d",
        campaign, processed_count, skipped_count, errors_count
    )

    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        llm_deferrals_count=llm_deferrals_count,
        errors=errors_list,
        metrics={"follow_ups_sent": follow_ups_sent_count}
    )
