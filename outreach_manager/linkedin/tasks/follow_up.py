# outreach_manager/linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up in a batch for all eligible CONNECTED deals."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from outreach_manager.crm.models import DealState
from outreach_manager.linkedin.browser.ui_validation import verify_ui_ready
from outreach_manager.linkedin.models import ActionLog

logger = logging.getLogger(__name__)

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
    from outreach_manager.crm.models import Deal

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


def _next_followup_deal(campaign):
    """Oldest CONNECTED deal in *campaign* not on a nudge cooldown."""
    for deal in _connected_deals(campaign):
        if not _too_soon_to_nudge(deal):
            return deal
    return None


def handle_follow_up(task, session, qualifiers) -> bool:
    from linkedin_cli.actions.message import send_raw_message
    from outreach_manager.core.agents.follow_up import run_follow_up_agent
    from outreach_manager.core.db.deals import capture_and_contribute, set_profile_state
    from outreach_manager.core.db.summaries import materialize_profile_summary_if_missing
    from outreach_manager.linkedin.ml.qualifier import validate_and_sanitize_message
    from outreach_manager.linkedin.scheduler import claim_due_deal, log_execution, schedule_next_action

    campaign = session.campaign

    processed_count = 0
    skipped_count = 0
    errors_count = 0
    llm_deferrals_count = 0
    errors_list: list[str] = []

    while session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        deal = claim_due_deal(campaign, [DealState.CONNECTED], "FOLLOW_UP")
        if deal is None:
            break

        if _too_soon_to_nudge(deal):
            next_t = schedule_next_action(deal, "nudge_cooldown")
            log_execution(deal, "FOLLOW_UP", "In Nudge Cooldown", "FOLLOW_UP", next_t)
            skipped_count += 1
            continue

        public_id = deal.lead.public_identifier
        logger.info(
            "[%s] %s %s",
            campaign, colored("▶ follow_up", "green", attrs=["bold"]), public_id,
        )

        deal_retry_count = 0
        cached_decision = None

        while deal_retry_count <= 1:
            try:
                verify_ui_ready(session, deal)

                capture_and_contribute(deal.lead, session)
                materialize_profile_summary_if_missing(deal, session)

                if cached_decision is None:
                    decision = run_follow_up_agent(session, deal)
                    cached_decision = decision
                else:
                    decision = cached_decision

                profile = _build_send_profile(deal)

                if decision.action == "send_message" and decision.message:
                    is_valid, clean_msg = validate_and_sanitize_message(decision.message)
                    if not is_valid:
                        logger.warning("[%s] follow_up: sanitizer rejected message for %s — skipping", campaign, public_id)
                        next_t = schedule_next_action(deal, "failed")
                        log_execution(deal, "FOLLOW_UP", "Sanitizer Rejected Message", "RETRY_FOLLOW_UP", next_t)
                        skipped_count += 1
                        break

                    sent = send_raw_message(session, profile, clean_msg)
                    if not sent:
                        next_t = schedule_next_action(deal, "failed")
                        log_execution(deal, "FOLLOW_UP", "Send Failed (CLI)", "RETRY_FOLLOW_UP", next_t)
                        skipped_count += 1
                        break

                    session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, campaign)

                    logger.info("[%s] follow_up sent to %s", campaign, public_id)
                    from outreach_manager.linkedin.db.chat import sync_conversation
                    try:
                        sync_conversation(session, public_id)
                    except Exception:
                        logger.exception("post-send sync failed for %s (best-effort)", public_id)

                    next_t = schedule_next_action(deal, "send_message")
                    log_execution(deal, "FOLLOW_UP", "Message Sent", "FOLLOW_UP", next_t)
                    processed_count += 1
                    break

                elif decision.action == "mark_completed":
                    set_profile_state(session, public_id, DealState.COMPLETED.value, outcome=decision.outcome)
                    schedule_next_action(deal, "mark_completed")
                    log_execution(deal, "FOLLOW_UP", f"Marked Completed ({decision.outcome})", "NONE", None)
                    logger.info("[%s] follow_up completed for %s: outcome=%s", campaign, public_id, decision.outcome)
                    processed_count += 1
                    break

                elif decision.action == "wait":
                    next_t = schedule_next_action(deal, "wait")
                    log_execution(deal, "FOLLOW_UP", "Agent Decided Wait", "FOLLOW_UP", next_t)
                    skipped_count += 1
                    break

                else:
                    next_t = schedule_next_action(deal, "failed")
                    log_execution(deal, "FOLLOW_UP", "Unknown Decision Action", "RETRY_FOLLOW_UP", next_t)
                    skipped_count += 1
                    break

            except Exception as exc:
                from outreach_manager.core.llm import is_quota_error
                if is_quota_error(exc):
                    provider = getattr(exc, "provider", "LLM Provider")
                    logger.info(
                        "[INFO] LLM temporarily unavailable.\n  Provider: %s\n  Reason: Quota exhausted\n  Deal '%s' deferred.",
                        provider, public_id,
                    )
                    next_t = schedule_next_action(deal, "error")
                    log_execution(deal, "FOLLOW_UP", "LLM Quota Deferred", "RETRY_FOLLOW_UP", next_t)
                    skipped_count += 1
                    llm_deferrals_count += 1
                    break

                if deal_retry_count == 0:
                    try:
                        session.ensure_browser()
                        is_healthy = getattr(session, "is_browser_healthy", lambda: True)()
                        if is_healthy:
                            logger.info(
                                "[INFO] Browser recovered. Retrying Deal '%s' once (preserving generated message).",
                                public_id,
                            )
                            deal_retry_count += 1
                            continue
                    except Exception as rec_err:
                        logger.debug("Browser recovery check failed during Deal retry: %s", rec_err)

                logger.warning("follow_up batch error for %s: %s", public_id, exc)
                next_t = schedule_next_action(deal, "failed")
                log_execution(deal, "FOLLOW_UP", f"Batch Error: {exc}", "RETRY_FOLLOW_UP", next_t)
                errors_count += 1
                errors_list.append(f"follow_up error for {public_id}: {exc}")
                break

    logger.info(
        "[%s] Follow-Up Workflow — Processed: %d, Skipped: %d, Errors: %d",
        campaign, processed_count, skipped_count, errors_count
    )

    from outreach_manager.core.workflow_result import WorkflowResult
    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        llm_deferrals_count=llm_deferrals_count,
        errors=errors_list,
    )
