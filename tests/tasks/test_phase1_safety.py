# tests/tasks/test_phase1_safety.py
"""
Phase 1 Safety & Idempotency Repair — regression tests.

Tests exactly the six invariants from the spec:
  1. Old inbound msg DOES NOT trigger another reply
  2. New inbound msg DOES trigger reply processing
  3. first_message_sent_at persisted despite sync failure
  4. Legacy deal with ChatMessage but NULL first_message_sent_at is protected
  5. Follow-up placeholder message blocked by sanitizer
  6. Failed follow-up send preserves CONNECTED
  7. FollowUpDecision valid/invalid form variants
"""
import pytest
from unittest.mock import MagicMock, patch
from django.utils import timezone

from outreach_manager.chat.models import ChatMessage
from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.agents.follow_up import FollowUpDecision
from outreach_manager.linkedin.db.chat import ConversationSyncResult, sync_conversation
from outreach_manager.linkedin.tasks.first_message import handle_first_message, _already_messaged
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.reply import handle_reply_unread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_connected_deal(session, public_id="lead-phase1"):
    """Create a minimal CONNECTED Deal with a matching Lead."""
    from outreach_manager.crm.models import Lead
    from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
    from outreach_manager.core.db.deals import set_profile_state

    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, {
        "first_name": "Test", "last_name": "Lead",
        "headline": "CTO", "positions": [{"company_name": "Acme"}],
    })
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, DealState.CONNECTED.value)
    return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)


def _chat_msg(deal, session, is_outgoing: bool, content="Hello") -> ChatMessage:
    """Create a ChatMessage for the given deal."""
    from django.utils import timezone as tz
    return ChatMessage.objects.create(
        deal=deal,
        linkedin_urn=f"urn:li:test:{deal.pk}:{is_outgoing}:{content[:8]}",
        content=content,
        is_outgoing=is_outgoing,
        owner=session.django_user,
        creation_date=tz.now(),
    )


# ---------------------------------------------------------------------------
# Problem 1 + 2: sync_conversation new_messages contract
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_old_inbound_msg_does_not_trigger_reply(fake_session):
    """Test 1: Old inbound messages must NOT cause REPLY_UNREAD to send a reply."""
    deal = _make_connected_deal(fake_session, "lead-reply-old")

    # Plant an existing inbound message directly in the DB (already synced before).
    _chat_msg(deal, fake_session, is_outgoing=False, content="Hey I am interested")

    # sync_conversation returns zero new_messages (nothing discovered this cycle).
    empty_result = ConversationSyncResult(
        messages=[{"is_outgoing": False, "text": "Hey I am interested"}],
        new_messages=[],   # <--- the critical part: nothing new
    )

    agent_called = []

    with patch(
        "outreach_manager.linkedin.tasks.reply.sync_conversation",
        return_value=empty_result,
    ), patch(
        "outreach_manager.linkedin.tasks.reply.run_follow_up_agent",
        side_effect=lambda *a, **kw: agent_called.append(1) or MagicMock(action="wait"),
    ):
        result = handle_reply_unread(None, fake_session, None)

    assert bool(result) is False, "reply should NOT have been sent"
    assert agent_called == [], "follow-up agent must NOT have been invoked for stale inbound"


@pytest.mark.django_db
def test_new_inbound_msg_triggers_reply(fake_session):
    """Test 2: A genuinely new inbound message MUST allow reply processing."""
    deal = _make_connected_deal(fake_session, "lead-reply-new")

    # Simulate a new inbound ChatMessage ORM object
    new_msg = _chat_msg(deal, fake_session, is_outgoing=False, content="New reply from lead")

    result_with_new = ConversationSyncResult(
        messages=[{"is_outgoing": False, "text": "New reply from lead"}],
        new_messages=[new_msg],  # <-- new inbound
    )

    agent_called = []

    with patch(
        "outreach_manager.linkedin.tasks.reply.sync_conversation",
        return_value=result_with_new,
    ), patch(
        "outreach_manager.linkedin.tasks.reply.run_follow_up_agent",
        side_effect=lambda *a, **kw: agent_called.append(1) or MagicMock(
            action="send_message", message="Great to hear!"
        ),
    ), patch(
        "outreach_manager.linkedin.tasks.reply.validate_and_sanitize_message",
        return_value=(True, "Great to hear!"),
    ), patch(
        "outreach_manager.linkedin.tasks.reply.send_raw_message",
        return_value=True,
    ), patch(
        "outreach_manager.linkedin.tasks.reply.materialize_profile_summary_if_missing",
    ):
        result = handle_reply_unread(None, fake_session, None)

    assert agent_called, "follow-up agent MUST have been invoked for new inbound message"


# ---------------------------------------------------------------------------
# Problem 2: first_message_sent_at idempotency
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_first_message_sent_at_persisted_despite_sync_failure(fake_session):
    """Test 3: first_message_sent_at is written even if post-send sync raises."""
    deal = _make_connected_deal(fake_session, "lead-fm-sync-fail")

    with patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        return_value=True,
    ), patch(
        "outreach_manager.linkedin.db.chat.sync_conversation",
        side_effect=Exception("Voyager timeout"),
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.generate_first_message",
        return_value="Hi there, excited to connect!",
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.validate_and_sanitize_message",
        return_value=(True, "Hi there, excited to connect!"),
    ), patch(
        "outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing",
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is True
    deal.refresh_from_db()
    assert deal.first_message_sent_at is not None, (
        "first_message_sent_at MUST be set even when post-send sync raises"
    )


@pytest.mark.django_db
def test_first_message_not_sent_again_after_sent_at_set(fake_session):
    """Test 3b: A subsequent FIRST_MESSAGE run skips the deal once first_message_sent_at is set."""
    deal = _make_connected_deal(fake_session, "lead-fm-no-dup")
    deal.first_message_sent_at = timezone.now()
    deal.save(update_fields=["first_message_sent_at"])

    send_called = []
    with patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        side_effect=lambda *a, **kw: send_called.append(1),
    ):
        result = handle_first_message(None, fake_session, None)

    assert bool(result) is False
    assert send_called == [], "send_raw_message must NOT be called when first_message_sent_at is set"


@pytest.mark.django_db
def test_legacy_deal_with_chatmessage_is_protected(fake_session):
    """Test 4: Legacy deal with NULL first_message_sent_at but existing ChatMessage is protected."""
    deal = _make_connected_deal(fake_session, "lead-legacy")
    assert deal.first_message_sent_at is None  # pre-migration state

    # Add an existing ChatMessage as would exist for pre-migration deals
    _chat_msg(deal, fake_session, is_outgoing=True, content="First message already sent historically")

    send_called = []
    with patch(
        "outreach_manager.linkedin.tasks.first_message.send_raw_message",
        side_effect=lambda *a, **kw: send_called.append(1),
    ):
        result = handle_first_message(None, fake_session, None)

    assert send_called == [], "Legacy deal with ChatMessage history MUST NOT receive another first message"
    assert bool(result) is False


# ---------------------------------------------------------------------------
# Problem 4: FOLLOW_UP sanitizer
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_follow_up_sanitizer_blocks_placeholder(fake_session):
    """Test 5: A generated follow-up with [Name] placeholder must not reach send_raw_message."""
    deal = _make_connected_deal(fake_session, "lead-sanitize")

    send_called = []

    with patch(
        # run_follow_up_agent is imported as-from inside handle_follow_up;
        # patch where it actually lives.
        "outreach_manager.core.agents.follow_up.run_follow_up_agent",
        return_value=MagicMock(
            action="send_message",
            message="Hi [Name], just following up!",
        ),
    ), patch(
        # send_raw_message imported from linkedin_cli.actions.message
        "linkedin_cli.actions.message.send_raw_message",
        side_effect=lambda *a, **kw: send_called.append(1) or True,
    ), patch(
        "outreach_manager.core.db.deals.capture_and_contribute",
    ), patch(
        "outreach_manager.core.db.summaries.materialize_profile_summary_if_missing",
    ):
        result = handle_follow_up(None, fake_session, None)

    assert send_called == [], "send_raw_message MUST NOT be called when message has placeholder"
    assert bool(result) is False


# ---------------------------------------------------------------------------
# Problem 5: CONNECTED must not be demoted on send failure
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_failed_followup_send_preserves_connected(fake_session):
    """Test 6: send_raw_message returning False must leave Deal.state = CONNECTED."""
    deal = _make_connected_deal(fake_session, "lead-send-fail")

    with patch(
        # run_follow_up_agent imported lazily inside the function
        "outreach_manager.core.agents.follow_up.run_follow_up_agent",
        return_value=MagicMock(
            action="send_message",
            message="Just checking in on our last chat!",
        ),
    ), patch(
        # validate_and_sanitize_message imported lazily from qualifier
        "outreach_manager.linkedin.ml.qualifier.validate_and_sanitize_message",
        return_value=(True, "Just checking in on our last chat!"),
    ), patch(
        # send_raw_message imported lazily from linkedin_cli
        "linkedin_cli.actions.message.send_raw_message",
        return_value=False,   # <-- simulate send failure
    ), patch(
        "outreach_manager.core.db.deals.capture_and_contribute",
    ), patch(
        "outreach_manager.core.db.summaries.materialize_profile_summary_if_missing",
    ):
        result = handle_follow_up(None, fake_session, None)

    deal.refresh_from_db()
    assert deal.state == DealState.CONNECTED, (
        f"Deal state MUST remain CONNECTED after send failure, got {deal.state!r}"
    )
    assert bool(result) is False


# ---------------------------------------------------------------------------
# Problem 3: FollowUpDecision schema
# ---------------------------------------------------------------------------

class TestFollowUpDecisionSchema:
    """Test 7: FollowUpDecision valid and invalid form variants."""

    def test_send_message_valid(self):
        d = FollowUpDecision(action="send_message", message="Hi there!", follow_up_hours=24.0)
        assert d.action == "send_message"
        assert d.message == "Hi there!"

    def test_send_message_requires_message(self):
        import pydantic
        with pytest.raises((pydantic.ValidationError, ValueError)):
            FollowUpDecision(action="send_message", message=None, follow_up_hours=24.0)

    def test_send_message_empty_string_rejected(self):
        import pydantic
        with pytest.raises((pydantic.ValidationError, ValueError)):
            FollowUpDecision(action="send_message", message="", follow_up_hours=24.0)

    def test_send_message_whitespace_only_rejected(self):
        import pydantic
        with pytest.raises((pydantic.ValidationError, ValueError)):
            FollowUpDecision(action="send_message", message="   ", follow_up_hours=24.0)

    def test_mark_completed_valid(self):
        d = FollowUpDecision(action="mark_completed", outcome="not_interested", follow_up_hours=0.0)
        assert d.action == "mark_completed"
        assert d.outcome == "not_interested"

    def test_mark_completed_requires_outcome(self):
        import pydantic
        with pytest.raises((pydantic.ValidationError, ValueError)):
            FollowUpDecision(action="mark_completed", outcome=None, follow_up_hours=0.0)

    def test_wait_valid_no_message_no_outcome(self):
        d = FollowUpDecision(action="wait", follow_up_hours=48.0)
        assert d.action == "wait"
        assert d.message is None
        assert d.outcome is None

    def test_wait_with_follow_up_hours(self):
        d = FollowUpDecision(action="wait", follow_up_hours=72.0)
        assert d.follow_up_hours == 72.0


# ---------------------------------------------------------------------------
# ConversationSyncResult contract unit tests
# ---------------------------------------------------------------------------

class TestConversationSyncResult:
    """Verify ConversationSyncResult structure is correct."""

    def test_default_empty(self):
        r = ConversationSyncResult()
        assert r.messages == []
        assert r.new_messages == []

    def test_with_data(self):
        msgs = [{"is_outgoing": False, "text": "hi"}]
        new = [object()]
        r = ConversationSyncResult(messages=msgs, new_messages=new)
        assert r.messages is msgs
        assert r.new_messages is new
