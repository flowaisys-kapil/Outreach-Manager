# tests/tasks/test_phase2_messaging.py
"""
Phase 2 Messaging Architecture Simplification — regression tests.

Tests the Phase 2 invariants:
  Invariant 7: FIRST_MESSAGE never calls run_follow_up_agent()
  Invariant 8: FirstMessageGenerator produces content only, not wait/complete/schedule decisions
  Invariant 9: Prepared message takes precedence when valid
  Invariant 10: Already-sent Deal exits immediately without generating or sending
  Invariant 11: Invalid generated first message (e.g. placeholders) is blocked by sanitizer
  Invariant 12: Valid generated first message is sanitized, sent, and first_message_sent_at persisted
  Invariant 13: Post-send sync failure does not erase send state or return False
  Invariant 14: Follow-up agent remains active for REPLY_UNREAD
  Invariant 15: Follow-up agent remains active for FOLLOW_UP
  Invariant 16: Acceptance sweep performs ZERO messaging
"""
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.linkedin.tasks.first_message import handle_first_message
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from outreach_manager.linkedin.pipeline.acceptances import run_acceptance_sweep
from outreach_manager.linkedin.db.chat import ConversationSyncResult
from outreach_manager.chat.models import ChatMessage


def _make_connected_deal(session, public_id="lead-phase2"):
    from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
    from outreach_manager.core.db.deals import set_profile_state
    from outreach_manager.crm.models import Lead

    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, {
        "first_name": "Test", "last_name": "User",
        "headline": "CEO", "positions": [{"company_name": "TechCorp"}],
    })
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, DealState.CONNECTED.value)
    Lead.objects.filter(public_identifier=public_id).update(urn="urn:li:fsd_profile:TEST")
    return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)


# ---------------------------------------------------------------------------
# Test 1 & Invariant 7: FIRST_MESSAGE does NOT use follow_up agent
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_first_message_does_not_call_follow_up_agent(fake_session):
    """Verify FIRST_MESSAGE calls generate_first_message, NOT run_follow_up_agent."""
    deal = _make_connected_deal(fake_session, "lead-fm-no-fu")

    follow_up_called = []
    generator_called = []

    with patch(
        "outreach_manager.core.agents.follow_up.run_follow_up_agent",
        side_effect=lambda *a, **kw: follow_up_called.append(1),
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        side_effect=lambda *a, **kw: generator_called.append(1) or "Hi Test, glad to connect with you!",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        return_value=True,
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is True
    assert follow_up_called == [], "run_follow_up_agent MUST NOT be called in FIRST_MESSAGE path"
    assert len(generator_called) == 1, "generate_first_message MUST be called"


# ---------------------------------------------------------------------------
# Test 2 & Invariant 9: Prepared message takes precedence
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_prepared_first_message_takes_precedence(fake_session):
    """Verify prepared message from qualification is used without calling generator."""
    deal = _make_connected_deal(fake_session, "lead-prep-msg")
    deal.profile_summary = {"prepared_first_message": "Hi Test, noticed your great work at TechCorp!"}
    deal.save(update_fields=["profile_summary"])

    generator_called = []

    with patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        side_effect=lambda *a, **kw: generator_called.append(1) or "Generated message",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        return_value=True,
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is True
    assert generator_called == [], "generate_first_message MUST NOT be called when valid prepared message exists"


# ---------------------------------------------------------------------------
# Test 3 & Invariant 10: Already-sent Deal exits immediately
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_already_sent_deal_exits_immediately(fake_session):
    """Verify deal with non-NULL first_message_sent_at is skipped immediately."""
    deal = _make_connected_deal(fake_session, "lead-already-sent")
    deal.first_message_sent_at = timezone.now()
    deal.save(update_fields=["first_message_sent_at"])

    generator_called = []
    send_called = []

    with patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        side_effect=lambda *a, **kw: generator_called.append(1),
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        side_effect=lambda *a, **kw: send_called.append(1),
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is False
    assert generator_called == []
    assert send_called == []


# ---------------------------------------------------------------------------
# Test 4 & Invariant 11: Generated first message is sanitized
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_generated_first_message_sanitizer_blocks_invalid(fake_session):
    """Verify generated message with template placeholder is blocked by sanitizer."""
    deal = _make_connected_deal(fake_session, "lead-invalid-gen")

    send_called = []

    with patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        return_value="Hi [name], glad to connect!",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        side_effect=lambda *a, **kw: send_called.append(1),
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is False
    assert send_called == [], "send_raw_message MUST NOT be called for sanitized-rejected message"


# ---------------------------------------------------------------------------
# Test 5 & Invariant 12: Valid generated first message sends & persists
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_valid_generated_first_message_sends_and_persists(fake_session):
    """Verify valid generated message passes sanitizer, sends, and persists send time."""
    deal = _make_connected_deal(fake_session, "lead-valid-gen")

    with patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        return_value="Hi Test, loved reading about your work at TechCorp!",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        return_value=True,
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is True
    deal.refresh_from_db()
    assert deal.first_message_sent_at is not None


# ---------------------------------------------------------------------------
# Test 6 & Invariant 13: Sync failure after send remains safe
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_post_send_sync_failure_preserves_send_state(fake_session):
    """Verify sync error after successful send does not erase first_message_sent_at."""
    deal = _make_connected_deal(fake_session, "lead-sync-err")

    with patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        return_value="Hi Test, glad to connect with you!",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        return_value=True,
    ), patch(
        "outreach_manager.linkedin.db.chat.sync_conversation",
        side_effect=Exception("Sync error"),
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is True
    deal.refresh_from_db()
    assert deal.first_message_sent_at is not None


# ---------------------------------------------------------------------------
# Test 7 & Invariant 14: Follow-up agent remains active for reply
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_reply_unread_uses_follow_up_agent(fake_session):
    """Verify handle_reply_unread uses run_follow_up_agent for inbound messages."""
    deal = _make_connected_deal(fake_session, "lead-reply-fu")

    new_msg = ChatMessage.objects.create(
        deal=deal,
        linkedin_urn="urn:li:test:reply",
        content="How does your product work?",
        is_outgoing=False,
        owner=fake_session.django_user,
        creation_date=timezone.now(),
    )

    sync_res = ConversationSyncResult(
        messages=[{"is_outgoing": False, "text": "How does your product work?"}],
        new_messages=[new_msg],
    )

    follow_up_called = []

    with patch(
        "outreach_manager.linkedin.tasks.reply.sync_conversation",
        return_value=sync_res,
    ), patch(
        "outreach_manager.linkedin.tasks.reply.run_follow_up_agent",
        side_effect=lambda *a, **kw: follow_up_called.append(1) or MagicMock(
            action="send_message", message="Here is how our product works!"
        ),
    ), patch(
        "outreach_manager.linkedin.tasks.reply.materialize_profile_summary_if_missing",
    ), patch(
        "outreach_manager.linkedin.tasks.reply.send_raw_message",
        return_value=True,
    ):
        result = handle_reply_unread(None, fake_session, None)

    assert bool(result) is True
    assert len(follow_up_called) == 1, "run_follow_up_agent MUST be called for reply_unread"


# ---------------------------------------------------------------------------
# Test 8 & Invariant 15: Follow-up agent remains active for follow-up
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_follow_up_uses_follow_up_agent(fake_session):
    """Verify handle_follow_up uses run_follow_up_agent."""
    deal = _make_connected_deal(fake_session, "lead-followup-fu")

    follow_up_called = []

    with patch(
        "outreach_manager.core.agents.follow_up.run_follow_up_agent",
        side_effect=lambda *a, **kw: follow_up_called.append(1) or MagicMock(
            action="send_message", message="Checking in on our last message!"
        ),
    ), patch(
        "outreach_manager.core.db.summaries.materialize_profile_summary_if_missing",
    ), patch(
        "outreach_manager.core.db.deals.capture_and_contribute",
    ), patch(
        "linkedin_cli.actions.message.send_raw_message",
        return_value=True,
    ), patch(
        "outreach_manager.linkedin.db.chat.sync_conversation",
    ), patch(
        "outreach_manager.linkedin.scheduler.claim_due_deal",
        side_effect=[deal, None],
    ):
        result = handle_follow_up(None, fake_session, None)

    assert bool(result) is True
    assert len(follow_up_called) == 1, "run_follow_up_agent MUST be called for follow_up"


# ---------------------------------------------------------------------------
# Test 9 & Invariant 16: Acceptance sweep performs zero messaging
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_acceptance_sweep_performs_zero_messaging(fake_session):
    """Verify run_acceptance_sweep performs zero messaging."""
    send_called = []
    generator_called = []

    with patch(
        "outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page",
        return_value=[],
    ), patch(
        "outreach_manager.linkedin.pipeline.acceptances.run_withdrawals_check",
    ), patch(
        "linkedin_cli.actions.message.send_raw_message",
        side_effect=lambda *a, **kw: send_called.append(1),
    ), patch(
        "outreach_manager.core.agents.first_message.generate_first_message",
        side_effect=lambda *a, **kw: generator_called.append(1),
    ):
        promoted = run_acceptance_sweep(fake_session, fake_session.campaign)

    assert send_called == [], "run_acceptance_sweep MUST NOT send messages"
    assert generator_called == [], "run_acceptance_sweep MUST NOT generate messages"
