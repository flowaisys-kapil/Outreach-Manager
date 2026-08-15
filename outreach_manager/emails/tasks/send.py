# openoutreach/emails/tasks/send.py
"""EMAIL task — sends the single Layer-1 email for a deal at READY_TO_EMAIL.

Layer 1 is outbound-only and single-shot: the daemon sends one email per
email-reachable lead and never re-emails (follow-ups + replies are the hosted
Layer-2 backend's job, reconstructed straight from the mailbox). So the whole
task is: pick the oldest queued deal + an under-cap box, let the agent compose,
send over SMTP, and record the send on the Deal — which moves it to EMAILED.

Each concern lives where it's cohesive; this module is just the orchestration:
  - the queue (one FSM state)  → ``core.db.deals.get_emailable_deals``
  - the per-box daily cap       → ``emails.models.Mailbox`` pacing manager
  - SMTP transport              → ``emails.sender.send_email``
"""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

from outreach_manager.crm.models import DealState
from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)


def handle_email(task, session, qualifiers) -> WorkflowResult:
    from outreach_manager.core.agents.email_opener import compose_opener_email
    from outreach_manager.core.db.deals import get_emailable_deals
    from outreach_manager.core.db.summaries import materialize_profile_summary_if_missing
    from outreach_manager.emails.models import Mailbox
    from outreach_manager.emails.sender import send_email

    campaign = session.campaign

    # Discovery
    mailbox = Mailbox.objects.least_loaded_under_cap()
    deal = get_emailable_deals(session).first() if mailbox else None

    logger.info(
        "[%s] Email Workflow — Candidates discovered: %d",
        campaign,
        1 if deal else 0,
    )

    if mailbox is None or deal is None:
        logger.info("[%s] email: nothing to send (empty queue or every box at cap)", campaign)
        return WorkflowResult(
            processed_count=0,
            skipped_count=1,
            error_count=0,
            errors=[]
        )

    public_id = deal.lead.public_identifier
    logger.info("[%s] Processing candidate: %s", campaign, public_id)

    try:
        materialize_profile_summary_if_missing(deal, session)
        draft = compose_opener_email(session, deal)

        # Action: Send email
        message_id = send_email(mailbox, deal.lead.api_email, draft.subject, draft.body)
        logger.info("[%s] Action executed: Opener email sent successfully", campaign)

        # State synchronization
        _record_sent_email(deal, mailbox, draft.subject, message_id)
        logger.info("[%s] State synchronized for: %s", campaign, public_id)

        logger.info(
            "[%s] Email Workflow Complete — Processed: 1, Skipped: 0, Errors: 0",
            campaign,
        )

        return WorkflowResult(
            processed_count=1,
            skipped_count=0,
            error_count=0,
            errors=[],
            metrics={"emails_sent": 1}
        )

    except Exception as exc:
        logger.exception("Email task error for %s: %s", public_id, exc)
        return WorkflowResult(
            processed_count=0,
            skipped_count=0,
            error_count=1,
            errors=[f"Email send error for {public_id}: {exc}"]
        )


def _record_sent_email(deal, mailbox, subject, message_id) -> None:
    """Bind the box, stamp the email fields, and move the deal to EMAILED — one save.

    The send record and the state transition live on the same row, so a single
    write commits both: the email can never be sent without leaving READY_TO_EMAIL
    (no double-send window), and EMAILED is never set without its audit fields.
    The body is not stored — Layer 2 reconstructs the thread from the mailbox.
    """
    deal.mailbox = mailbox
    deal.email_subject = subject
    deal.email_message_id = message_id
    deal.email_sent_at = timezone.now()
    deal.state = DealState.EMAILED
    deal.save(update_fields=[
        "mailbox", "email_subject", "email_message_id", "email_sent_at", "state",
    ])
    try:
        from outreach_manager.crm.models.event_log import EventLog
        EventLog.objects.create(
            campaign=deal.campaign,
            deal=deal,
            event_type=EventLog.EventType.EMAIL_SENT,
            detail=f"Email sent to {deal.lead.api_email}: {subject}"
        )
    except Exception as e:
        logger.warning("Failed to log email_sent event: %s", e)
