# outreach_manager/linkedin/tasks/reply.py
"""Batch workflow handler for checking unread inbox messages and replying to all eligible deals."""
import logging
from termcolor import colored

from linkedin_cli.actions.message import send_raw_message
from outreach_manager.crm.models import Deal, DealState
from outreach_manager.linkedin.db.chat import sync_conversation
from outreach_manager.core.agents.follow_up import run_follow_up_agent
from outreach_manager.core.db.summaries import materialize_profile_summary_if_missing
from outreach_manager.linkedin.ml.qualifier import validate_and_sanitize_message
from outreach_manager.linkedin.browser.ui_validation import verify_ui_ready
from outreach_manager.linkedin.models import ActionLog

logger = logging.getLogger(__name__)


def handle_reply_unread(task, session, qualifiers) -> bool:
    """Checks for unread incoming messages for active connected deals and generates AI replies in a batch.

    Isolation Boundary:
    - ONLY reads inbox threads.
    - ONLY replies to inbound unread messages.
    - NEVER sends connection requests or initiates new conversations.
    """
    campaign = session.campaign

    from django.db.models import Q
    active_deals = list(Deal.objects.filter(
        Q(outcome="") | Q(outcome__isnull=True),
        campaign=campaign,
        state=DealState.CONNECTED,
        lead__disqualified=False,
    ).select_related("lead"))

    eligible_count = len(active_deals)
    if eligible_count == 0:
        logger.info("[%s] reply_unread: no active CONNECTED deals — slot skipped", campaign)
        from outreach_manager.core.workflow_result import WorkflowResult
        return WorkflowResult()

    errors_list: list[str] = []
    processed_count = 0
    skipped_count = 0
    errors_count = 0
    llm_deferrals_count = 0

    for deal in active_deals:
        public_id = deal.lead.public_identifier
        deal_retry_count = 0
        cached_decision = None

        while deal_retry_count <= 1:
            try:
                verify_ui_ready(session, deal)

                result = sync_conversation(session, public_id, allow_navigation=False)

                has_new_inbound = any(
                    not m.is_outgoing
                    for m in result.new_messages
                )

                if has_new_inbound:
                    logger.info("[%s] %s %s", campaign, colored("▶ reply_unread", "yellow", attrs=["bold"]), public_id)
                    materialize_profile_summary_if_missing(deal, session)

                    if cached_decision is None:
                        decision = run_follow_up_agent(session, deal)
                        cached_decision = decision
                    else:
                        decision = cached_decision

                    if decision.action == "send_message" and decision.message:
                        is_valid, clean_msg = validate_and_sanitize_message(decision.message)
                        if not is_valid:
                            logger.warning(
                                "[%s] reply_unread: sanitizer rejected reply for %s — skipping",
                                campaign, public_id
                            )
                            skipped_count += 1
                            break

                        profile = {
                            "public_identifier": public_id,
                            "urn": deal.lead.urn or "",
                        }
                        sent = send_raw_message(session, profile, clean_msg)
                        if sent:
                            session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, campaign)
                            from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                            next_t = schedule_next_action(deal, "send_message")
                            log_execution(deal, "REPLY_UNREAD", "Replied to Inbound Message", "FOLLOW_UP", next_t)
                            processed_count += 1
                            break
                        else:
                            skipped_count += 1
                            break
                    else:
                        skipped_count += 1
                        break
                else:
                    skipped_count += 1
                    break

            except Exception as e:
                from outreach_manager.core.llm import is_quota_error
                if is_quota_error(e):
                    provider = getattr(e, "provider", "LLM Provider")
                    logger.info(
                        "[INFO] LLM temporarily unavailable.\n  Provider: %s\n  Reason: Quota exhausted\n  Reply for '%s' deferred.",
                        provider, public_id,
                    )
                    from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                    next_t = schedule_next_action(deal, "error")
                    log_execution(deal, "REPLY_UNREAD", "LLM Quota Deferred", "RETRY_REPLY", next_t)
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

                logger.warning("reply_unread error for %s: %s", public_id, e)
                errors_count += 1
                errors_list.append(f"reply_unread error for {public_id}: {e}")
                break

    logger.info(
        "[%s] Reply Workflow — Eligible: %d, Processed: %d, Skipped: %d, Errors: %d",
        campaign, eligible_count, processed_count, skipped_count, errors_count
    )

    from outreach_manager.core.workflow_result import WorkflowResult
    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        llm_deferrals_count=llm_deferrals_count,
        errors=errors_list,
    )
