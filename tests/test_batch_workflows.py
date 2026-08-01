# tests/test_batch_workflows.py
"""Unit and batch execution tests for Ticket 3 — Batch Workflows."""
import pytest
from unittest.mock import patch, MagicMock
from django.utils import timezone
from datetime import timedelta

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.models import Task
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending
from outreach_manager.linkedin.tasks.first_message import handle_first_message
from outreach_manager.linkedin.tasks.connect import handle_connect
from outreach_manager.linkedin.tasks.extract_leads import handle_extract_leads


@pytest.mark.django_db
class TestBatchWorkflows:

    def _make_deal(self, fake_session, pid, state):
        url = f"https://www.linkedin.com/in/{pid}/"
        create_enriched_lead(fake_session, url, {"first_name": pid, "last_name": "Test"})
        promote_lead_to_deal(fake_session, pid)
        set_profile_state(fake_session, pid, state.value)
        deal = Deal.objects.get(lead__public_identifier=pid, campaign=fake_session.campaign)
        return deal

    def test_reply_unread_batch_multiple_deals(self, fake_session):
        """Reply workflow processes ALL eligible deals in batch, not just the first one."""
        deal1 = self._make_deal(fake_session, "lead-reply-1", DealState.CONNECTED)
        deal2 = self._make_deal(fake_session, "lead-reply-2", DealState.CONNECTED)

        mock_sync_1 = MagicMock(new_messages=[MagicMock(is_outgoing=False)])
        mock_sync_2 = MagicMock(new_messages=[MagicMock(is_outgoing=False)])

        def mock_sync_func(session, public_id, allow_navigation=False):
            if "lead-reply-1" in public_id:
                return mock_sync_1
            return mock_sync_2

        mock_decision = MagicMock(action="send_message", message="Hello, thank you for reaching out to us today!")

        with patch("outreach_manager.linkedin.tasks.reply.sync_conversation", side_effect=mock_sync_func), \
             patch("outreach_manager.linkedin.tasks.reply.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.linkedin.tasks.reply.run_follow_up_agent", return_value=mock_decision), \
             patch("outreach_manager.linkedin.tasks.reply.send_raw_message", return_value=True) as mock_send:

            result = handle_reply_unread(None, fake_session, qualifiers={})

            assert bool(result) is True
            # Both deals were sent messages in a single batch execution!
            assert mock_send.call_count == 2

    def test_follow_up_batch_resilience_error_handling(self, fake_session):
        """Deal 2 throwing exception does NOT stop Deal 3 from executing.

        With Ticket 9 deal-local retry logic, a browser error on Deal 2 triggers
        a recovery attempt. We mock is_browser_healthy=False so recovery is skipped
        and deal2 gets exactly one LLM call (no retry), keeping call_count==3.
        """
        deal1 = self._make_deal(fake_session, "lead-fu-1", DealState.CONNECTED)
        deal2 = self._make_deal(fake_session, "lead-fu-2", DealState.CONNECTED)
        deal3 = self._make_deal(fake_session, "lead-fu-3", DealState.CONNECTED)

        # Force due status for all 3 deals
        Deal.objects.all().update(next_action_at=timezone.now() - timedelta(days=1))

        mock_decision = MagicMock(action="send_message", message="Hello, thank you for following up with us today!")
        call_count = 0

        def mock_agent(session, deal):
            nonlocal call_count
            call_count += 1
            if deal.lead.public_identifier == "lead-fu-2":
                raise RuntimeError("LLM Agent Failure on Deal 2")
            return mock_decision

        with patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", side_effect=mock_agent), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"), \
             patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal1, deal2, deal3, None]), \
             patch.object(fake_session, "is_browser_healthy", return_value=False):

            result = handle_follow_up(None, fake_session, qualifiers={})

            assert bool(result) is True
            # Attempted all 3 deals despite deal 2 failing.
            # is_browser_healthy=False so no retry → deal2 gets exactly 1 LLM call.
            assert call_count == 3
            # Sent 2 successful messages (deal 1 and deal 3)
            assert mock_send.call_count == 2

    def test_first_message_batch_multiple_deals(self, fake_session):
        """First message workflow delivers messages to multiple unmessaged connected deals."""
        deal1 = self._make_deal(fake_session, "lead-fm-1", DealState.CONNECTED)
        deal2 = self._make_deal(fake_session, "lead-fm-2", DealState.CONNECTED)

        Deal.objects.all().update(next_action_at=timezone.now() - timedelta(days=1))

        with patch("outreach_manager.linkedin.tasks.first_message._already_messaged", return_value=False), \
             patch("outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.linkedin.tasks.first_message.generate_first_message", return_value="Hello, glad to connect with you today!"), \
             patch("outreach_manager.linkedin.tasks.first_message.send_raw_message", return_value=True) as mock_send, \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            result = handle_first_message(None, fake_session, qualifiers={})

            assert bool(result) is True
            assert mock_send.call_count == 2
            deal1.refresh_from_db()
            deal2.refresh_from_db()
            assert deal1.first_message_sent_at is not None
            assert deal2.first_message_sent_at is not None

    def test_check_pending_batch(self, fake_session):
        """Check pending processes all due pending deals in batch."""
        deal1 = self._make_deal(fake_session, "lead-cp-1", DealState.PENDING)
        deal2 = self._make_deal(fake_session, "lead-cp-2", DealState.PENDING)

        Deal.objects.all().update(next_check_pending_at=timezone.now() - timedelta(days=1))

        with patch("outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page", return_value={"lead-cp-1"}), \
             patch("outreach_manager.linkedin.tasks.check_pending._resolve_status_individually", return_value="PENDING"), \
             patch("outreach_manager.linkedin.pipeline.acceptances.run_withdrawals_check"):

            result = handle_check_pending(None, fake_session, qualifiers={})

            assert bool(result) is True
            deal1.refresh_from_db()
            deal2.refresh_from_db()
            assert deal1.state == DealState.CONNECTED.value
            assert deal2.state == DealState.PENDING.value
            assert deal2.backoff_hours == 48  # Doubled from 24h default

    def test_extract_leads_batch(self, fake_session):
        """Extract leads iterates through qualify_gen batch."""
        def mock_gen():
            yield "pid-1"
            yield "pid-2"
            yield "pid-3"

        mock_qualifier = MagicMock()

        with patch("outreach_manager.linkedin.pipeline.pools.qualify_source", return_value=mock_gen()):
            result = handle_extract_leads(None, fake_session, qualifiers={fake_session.campaign.pk: mock_qualifier})
            assert bool(result) is True
