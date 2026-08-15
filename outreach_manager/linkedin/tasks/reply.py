# openoutreach/linkedin/tasks/reply.py
"""Reply Workflow — Simplified, LinkedIn-First Architecture.

Architecture & Ownership Model
--------------------------------
1. Every workflow is independent.
   No module-level imports from connect, follow_up, first_message, check_pending, extract_leads, email.
2. LinkedIn is the source of truth for live conversation state.
   Live conversation state comes directly from LinkedIn (discover_unread_conversations, read_conversation_thread).
3. Single LLM call per conversation.
   The LLM decides whether a reply is required, what to say, or whether to wait/conclude.
4. Database synchronization occurs AFTER execution for persistence and reporting, never before.
"""
from __future__ import annotations

import logging
from typing import Any

from linkedin_cli.actions.message import send_raw_message
from outreach_manager.linkedin.browser.messaging import (
    discover_unread_conversations,
    read_conversation_thread,
)
from outreach_manager.linkedin.db.chat import sync_conversation
from outreach_manager.core.agents.reply_agent import run_reply_agent

run_follow_up_agent = run_reply_agent

from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)


def materialize_profile_summary_if_missing(*args, **kwargs):
    """Compatibility alias for tests mocking summary materialization."""
    return True


def verify_ui_ready(*args, **kwargs):
    """Compatibility alias for tests mocking UI verification."""
    return True



def handle_reply_unread(task, session, qualifiers=None) -> WorkflowResult:
    """Process unread LinkedIn conversations using a single LLM call per conversation.

    Execution Flow:
        1. Discover unread conversations directly from LinkedIn.
        2. For each conversation:
            a. Load complete visible conversation thread.
            b. Single LLM call to decide action and message content.
            c. Send reply on LinkedIn if action is send_message/send_reply.
            d. Synchronize conversation to database post-execution.
        3. Return operational WorkflowResult.
    """
    campaign = getattr(session, "campaign", None)

    # ── Stage 1: Unread Discovery ─────────────────────────────────────────────
    unread_convs = discover_unread_conversations(session)
    unread_count = len(unread_convs)

    logger.info("[%s] Reply Workflow — Candidates discovered: %d", campaign, unread_count)

    if unread_count == 0:
        logger.info(
            "[%s] Reply Workflow Complete — Processed: 0, Skipped: 0, Errors: 0",
            campaign,
        )
        return WorkflowResult(processed_count=0, skipped_count=0, error_count=0)

    replies_sent_count = 0
    skipped_count = 0
    errors_count = 0
    send_failures_count = 0
    llm_deferrals_count = 0
    errors_list: list[str] = []

    # ── Stage 2: Process Each Unread Conversation ─────────────────────────────
    for index, conv in enumerate(unread_convs, start=1):
        public_id = conv.get("public_identifier", "")
        conv_urn = conv.get("conversation_urn", "")
        target_urn = conv.get("target_urn", "")
        target_label = public_id or target_urn or f"conversation-{index}"

        logger.info("[%s] Processing candidate: %s", campaign, target_label)

        try:
            # Load complete conversation thread live from LinkedIn
            thread = read_conversation_thread(session, conv)

            # Single LLM call to decide action & reply content
            decision = run_follow_up_agent(session, conversation_history=thread)


            # Determine if LLM decided to send a reply
            is_send_action = decision.action in ("send_message", "send_reply")

            # Action execution
            if is_send_action and decision.message:
                profile_payload = {
                    "public_identifier": public_id,
                    "urn": target_urn or "",
                }
                sent = send_raw_message(session, profile_payload, decision.message)
                if sent:
                    logger.info("[%s] Action executed: Reply sent successfully", campaign)
                    replies_sent_count += 1
                    if getattr(session, "linkedin_profile", None) and campaign:
                        from outreach_manager.linkedin.models import ActionLog
                        session.linkedin_profile.record_action(
                            ActionLog.ActionType.REPLY, campaign
                        )
                else:
                    logger.warning("[%s] Action executed: Reply send failed", campaign)
                    send_failures_count += 1
            else:
                logger.info("[%s] Action executed: LLM decided not to reply", campaign)
                skipped_count += 1

            # State synchronization (Persistence post-execution)
            _sync_post_execution(session, campaign, public_id, conv_urn, target_label)
            logger.info("[%s] State synchronized for: %s", campaign, target_label)

        except Exception as exc:
            from outreach_manager.core.llm import is_quota_error

            if is_quota_error(exc):
                logger.info("[%s] LLM quota exhausted; deferring reply for %s", campaign, target_label)
                llm_deferrals_count += 1
                skipped_count += 1
            else:
                logger.warning("[%s] Error processing %s: %s", campaign, target_label, exc)
                errors_count += 1
                errors_list.append(f"reply error for {target_label}: {exc}")

    logger.info(
        "[%s] Reply Workflow Complete — Processed: %d, Skipped: %d, Errors: %d",
        campaign, unread_count, skipped_count, errors_count
    )

    return WorkflowResult(
        processed_count=unread_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        llm_deferrals_count=llm_deferrals_count,
        errors=errors_list,
        metrics={
            "replies_sent": replies_sent_count,
            "conversations_processed": unread_count,
            "conversations_skipped": skipped_count,
            "send_failures": send_failures_count,
        },
    )


def _sync_post_execution(
    session, campaign, public_id: str, conv_urn: str, target_label: str
) -> None:
    """Synchronize conversation to database after processing."""
    if not public_id or not campaign:
        return
    try:
        sync_conversation(
            session, public_id, allow_navigation=False, conversation_urn=conv_urn
        )
    except Exception as sync_err:
        logger.warning(
            "Database sync failed post-execution for '%s': %s",
            target_label,
            sync_err,
        )
