# openoutreach/linkedin/tasks/first_message.py
"""Single-purpose workflow handler for sending 1st messages to newly accepted connections in a batch."""
import logging
from termcolor import colored

from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.chat.models import ChatMessage
from linkedin_cli.actions.message import send_raw_message
from outreach_manager.core.agents.first_message import generate_first_message
from outreach_manager.linkedin.ml.qualifier import validate_and_sanitize_message
from outreach_manager.linkedin.models import ActionLog
from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)


def materialize_profile_summary_if_missing(*args, **kwargs):
    """Compatibility alias for tests mocking summary materialization."""
    return True


def verify_ui_ready(*args, **kwargs):
    """Compatibility alias for tests mocking UI verification."""
    return True



def _already_messaged(deal) -> bool:
    """Return True if this deal must NOT receive a first message.

    Two-tier guard:
      1. ``first_message_sent_at`` (new, durable): set immediately after a
         confirmed send. Authoritative for all sends after this field was added.
      2. ChatMessage existence (legacy): protects deals created before the field
         was added that already have conversation history.
    """
    if deal.first_message_sent_at is not None:
        return True
    if ChatMessage.objects.filter(deal=deal).exists():
        return True
    return False


def handle_first_message(task, session, qualifiers) -> WorkflowResult:
    """Delivers initial welcome/introductory message to all unmessaged CONNECTED leads in a batch."""
    from outreach_manager.linkedin.scheduler import claim_due_deal, log_execution, schedule_next_action

    campaign = session.campaign

    # Discovery Log
    unmessaged_deals = Deal.objects.filter(
        campaign=campaign,
        state=DealState.CONNECTED,
        first_message_sent_at__isnull=True,
    ).select_related("lead")
    due_deals_count = 0
    for d in unmessaged_deals:
        if not _already_messaged(d):
            due_deals_count += 1
    logger.info("[%s] First Message Workflow — Candidates discovered: %d", campaign, due_deals_count)

    processed_count = 0
    skipped_count = 0
    errors_count = 0
    llm_deferrals_count = 0
    errors_list: list[str] = []

    candidate_deals = list(unmessaged_deals)
    for target_deal in candidate_deals:
        if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
            break

        if _already_messaged(target_deal):
            next_t = schedule_next_action(target_deal, "already_messaged")
            log_execution(target_deal, "FIRST_MESSAGE", "Already Messaged", "FOLLOW_UP", next_t)
            skipped_count += 1
            continue

        public_id = target_deal.lead.public_identifier

        logger.info("[%s] Processing candidate: %s", campaign, public_id)

        try:
            summary = target_deal.profile_summary or {}
            prepared_msg = summary.get("prepared_first_message", "")
            message_text = ""

            if prepared_msg:
                is_valid, clean_msg = validate_and_sanitize_message(prepared_msg)
                if is_valid:
                    message_text = clean_msg

            if not message_text:
                raw_msg = generate_first_message(session, target_deal)
                is_valid, clean_msg = validate_and_sanitize_message(raw_msg)
                if is_valid:
                    message_text = clean_msg

            if not message_text:
                logger.warning(
                    "[%s] first_message: no valid message generated for %s (sanitizer rejected) — skipping",
                    campaign, public_id,
                )
                next_t = schedule_next_action(target_deal, "failed")
                log_execution(target_deal, "FIRST_MESSAGE", "Message Generation/Sanitizer Failed", "RETRY_FIRST_MESSAGE", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                skipped_count += 1
                continue

            profile = {
                "public_identifier": public_id,
                "urn": target_deal.lead.urn or "",
            }

            # Action: Send first message
            sent = send_raw_message(session, profile, message_text)
            if sent:
                session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, campaign)
                logger.info("[%s] Action executed: First message sent successfully", campaign)

                # State synchronization
                target_deal.first_message_sent_at = timezone.now()
                target_deal.save(update_fields=["first_message_sent_at"])

                from outreach_manager.linkedin.db.chat import sync_conversation
                try:
                    sync_conversation(session, public_id)
                except Exception:
                    logger.warning(
                        "[%s] first_message: post-send sync failed for %s — send record already persisted",
                        campaign, public_id,
                    )

                next_t = schedule_next_action(target_deal, "first_message_sent")
                log_execution(target_deal, "FIRST_MESSAGE", "Message Sent", "FOLLOW_UP", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                processed_count += 1
            else:
                next_t = schedule_next_action(target_deal, "failed")
                log_execution(target_deal, "FIRST_MESSAGE", "Send Failed (CLI)", "RETRY_FIRST_MESSAGE", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                skipped_count += 1

        except Exception as exc:
            from outreach_manager.core.llm import is_quota_error
            if is_quota_error(exc):
                provider = getattr(exc, "provider", "LLM Provider")
                logger.info(
                    "[INFO] LLM temporarily unavailable.\n  Provider: %s\n  Reason: Quota exhausted\n  Deal '%s' deferred.",
                    provider, public_id,
                )
                next_t = schedule_next_action(target_deal, "error")
                log_execution(target_deal, "FIRST_MESSAGE", "LLM Quota Deferred", "RETRY_FIRST_MESSAGE", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                skipped_count += 1
                llm_deferrals_count += 1
            else:
                logger.warning("first_message batch error for %s: %s", public_id, exc)
                next_t = schedule_next_action(target_deal, "failed")
                log_execution(target_deal, "FIRST_MESSAGE", f"Send Error: {exc}", "RETRY_FIRST_MESSAGE", next_t)
                logger.info("[%s] State synchronized for: %s", campaign, public_id)
                errors_count += 1
                errors_list.append(f"first_message error for {public_id}: {exc}")

    logger.info(
        "[%s] First Message Workflow Complete — Processed: %d, Skipped: %d, Errors: %d",
        campaign, processed_count, skipped_count, errors_count
    )

    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        llm_deferrals_count=llm_deferrals_count,
        errors=errors_list,
        metrics={"first_messages_sent": processed_count}
    )
