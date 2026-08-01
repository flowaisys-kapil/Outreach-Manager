# tests/test_scheduler_workflow_aware.py
"""Regression tests for Workflow-Aware Deal Claiming (Scheduler Bug Fix)."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import timedelta
from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.chat.models import ChatMessage
from outreach_manager.linkedin.scheduler import claim_due_deal, schedule_next_action
from outreach_manager.linkedin.tasks.first_message import handle_first_message
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up


@pytest.mark.django_db
class TestWorkflowAwareDealClaiming:

    def _make_deal(self, fake_session, pid, state=DealState.CONNECTED):
        url = f"https://www.linkedin.com/in/{pid}/"
        create_enriched_lead(fake_session, url, {"first_name": pid, "last_name": "Test"})
        promote_lead_to_deal(fake_session, pid)
        set_profile_state(fake_session, pid, state.value if hasattr(state, "value") else state)
        deal = Deal.objects.get(lead__public_identifier=pid, campaign=fake_session.campaign)
        deal.next_action_at = timezone.now() - timedelta(hours=1)
        deal.save()
        return deal

    def test_first_message_never_claims_already_messaged_deal_by_timestamp(self, fake_session):
        """FIRST_MESSAGE workflow never claims a Deal where first_message_sent_at is set."""
        deal = self._make_deal(fake_session, "messaged-by-ts", DealState.CONNECTED)
        deal.first_message_sent_at = timezone.now() - timedelta(minutes=10)
        deal.save()

        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FIRST_MESSAGE")
        assert claimed is None

    def test_first_message_never_claims_already_messaged_deal_by_chat_history(self, fake_session):
        """FIRST_MESSAGE workflow never claims a Deal with legacy ChatMessage history."""
        deal = self._make_deal(fake_session, "messaged-by-chat", DealState.CONNECTED)
        ChatMessage.objects.create(
            deal=deal,
            linkedin_urn="urn:li:msg:1",
            content="Hello there!",
            is_outgoing=True,
            owner=fake_session.django_user,
        )

        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FIRST_MESSAGE")
        assert claimed is None

    def test_first_message_claims_only_unmessaged_deal(self, fake_session):
        """FIRST_MESSAGE claims unmessaged CONNECTED deals."""
        unmessaged_deal = self._make_deal(fake_session, "unmessaged-lead", DealState.CONNECTED)

        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FIRST_MESSAGE")
        assert claimed is not None
        assert claimed.pk == unmessaged_deal.pk

    def test_follow_up_only_receives_messaged_deals(self, fake_session):
        """FOLLOW_UP claims only Deals that have received a first message."""
        unmessaged_deal = self._make_deal(fake_session, "unmessaged-deal", DealState.CONNECTED)
        messaged_deal = self._make_deal(fake_session, "messaged-deal", DealState.CONNECTED)
        messaged_deal.first_message_sent_at = timezone.now() - timedelta(days=2)
        messaged_deal.save()

        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP")
        assert claimed is not None
        assert claimed.pk == messaged_deal.pk

    def test_claim_due_deal_returns_none_when_no_matching_deal(self, fake_session):
        """claim_due_deal returns None when no Deal matches the task requirements."""
        self._make_deal(fake_session, "unmessaged-deal", DealState.CONNECTED)

        # No messaged deal exists for FOLLOW_UP
        claimed_fu = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP")
        assert claimed_fu is None

    def test_deal_cannot_be_reclaimed_in_session_after_schedule_next_action(self, fake_session):
        """A Deal cannot be returned twice during the same session after schedule_next_action advances it."""
        deal = self._make_deal(fake_session, "unmessaged-deal", DealState.CONNECTED)

        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FIRST_MESSAGE")
        assert claimed is not None
        assert claimed.pk == deal.pk

        # Simulate message send success and scheduling
        deal.first_message_sent_at = timezone.now()
        schedule_next_action(deal)

        # Immediate re-claim attempt returns None
        reclaimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FIRST_MESSAGE")
        assert reclaimed is None

    def test_workflow_batch_session_no_repeated_already_messaged_logs(self, fake_session):
        """Run FIRST_MESSAGE on a mixed pool of Deals (unmessaged, already messaged, follow-up).
        Verifies FIRST_MESSAGE processes ONLY unmessaged Deals and terminates cleanly without repeated 'Already Messaged' skips.
        """
        unmessaged_deal = self._make_deal(fake_session, "fm-unmessaged", DealState.CONNECTED)
        messaged_deal1 = self._make_deal(fake_session, "fm-messaged-1", DealState.CONNECTED)
        messaged_deal1.first_message_sent_at = timezone.now() - timedelta(days=1)
        messaged_deal1.save()

        messaged_deal2 = self._make_deal(fake_session, "fm-messaged-2", DealState.CONNECTED)
        ChatMessage.objects.create(
            deal=messaged_deal2,
            linkedin_urn="urn:li:msg:2",
            content="Existing chat",
            is_outgoing=True,
            owner=fake_session.django_user,
        )

        with patch("outreach_manager.linkedin.tasks.first_message.generate_first_message", return_value="Hi Test, glad to connect with you today on LinkedIn!"), \
             patch("outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.linkedin.tasks.first_message.send_raw_message", return_value=True) as mock_send, \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            result = handle_first_message(None, fake_session, qualifiers={})

            assert bool(result) is True
            # Exactly 1 message sent to the ONLY unmessaged deal!
            assert mock_send.call_count == 1
            unmessaged_deal.refresh_from_db()
            assert unmessaged_deal.first_message_sent_at is not None
