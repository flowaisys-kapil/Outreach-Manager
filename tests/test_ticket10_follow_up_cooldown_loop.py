# tests/test_ticket10_follow_up_cooldown_loop.py
"""Ticket 10 Regression Tests — Prevent Infinite FOLLOW_UP Batch Loop (Cooldown Reclaim Bug).

Verifies that:
  1. A Deal in nudge cooldown is processed exactly once per workflow session.
  2. Cooldown Deals cannot be reclaimed during the same workflow session.
  3. Message-sent Deals cannot be reclaimed.
  4. Skipped Deals (wait decision) cannot be reclaimed.
  5. When no eligible Deals remain, claim_due_deal() returns None.
  6. FOLLOW_UP exits naturally after processing the entire eligible pool.
  7. REPLY executes immediately after FOLLOW_UP completes.
  8. A mixed pool (send + cooldown + skipped + error) completes without infinite looping.
"""

import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.models import Task
from outreach_manager.chat.models import ChatMessage
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.scheduler import claim_due_deal, schedule_next_action
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from outreach_manager.core.agents.follow_up import FollowUpDecision


SAMPLE_PROFILE = {
    "first_name": "Test",
    "last_name": "Lead",
    "headline": "Software Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _make_connected_deal(session, public_id="lead1", message_hours_ago=1):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, DealState.CONNECTED.value)
    deal = Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)
    deal.first_message_sent_at = timezone.now() - timedelta(hours=message_hours_ago)
    deal.save(update_fields=["first_message_sent_at"])

    # Create initial outgoing message
    ChatMessage.objects.create(
        deal=deal,
        content="Hello from sales!",
        linkedin_urn=f"urn:li:msg:{public_id}",
        is_outgoing=True,
        creation_date=timezone.now() - timedelta(hours=message_hours_ago),
    )
    return deal


def _make_task(task_type, payload):
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


def _build_context(session):
    from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
    qualifier = BayesianQualifier(seed=42)
    qualifier.rank_profiles = lambda profiles, **kw: profiles
    return {session.campaign.pk: qualifier}


@pytest.mark.django_db
class TestTicket10CooldownReclaimPrevention:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_1_cooldown_deal_processed_exactly_once(self, fake_session):
        """1. A Deal in cooldown is processed exactly once by FOLLOW_UP."""
        deal = _make_connected_deal(fake_session, "cooldown1", message_hours_ago=1)
        # Simulate legacy/pending timestamp
        deal.next_check_pending_at = timezone.now() - timedelta(days=5)
        deal.save(update_fields=["next_check_pending_at"])

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        result = handle_follow_up(task, fake_session, _build_context(fake_session))

        assert result.processed_count == 0
        assert result.skipped_count == 1
        assert result.error_count == 0

        deal.refresh_from_db()
        assert deal.next_action_at > timezone.now()
        assert deal.claimed_at is None

    def test_2_cooldown_deals_cannot_be_reclaimed_in_same_workflow(self, fake_session):
        """2. Multiple cooldown Deals cannot be reclaimed during the same workflow."""
        _make_connected_deal(fake_session, "cool_a", message_hours_ago=1)
        _make_connected_deal(fake_session, "cool_b", message_hours_ago=2)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})

        claim_calls = 0
        original_claim = claim_due_deal

        def tracked_claim(*args, **kwargs):
            nonlocal claim_calls
            claim_calls += 1
            return original_claim(*args, **kwargs)

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=tracked_claim):
            result = handle_follow_up(task, fake_session, _build_context(fake_session))

        assert result.skipped_count == 2
        # Should be called 3 times total: cool_a, cool_b, then None
        assert claim_calls == 3

    @patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready")
    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin_cli.actions.message.send_raw_message", return_value=True)
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    def test_3_message_sent_deals_cannot_be_reclaimed(
        self, mock_capture, mock_agent, mock_send, mock_summary, mock_ui, fake_session
    ):
        """3. Message-sent Deals cannot be reclaimed in the same session."""
        deal = _make_connected_deal(fake_session, "send1", message_hours_ago=100)
        mock_agent.return_value = FollowUpDecision(
            action="send_message", message="Hello Alice, following up on our proposal!", follow_up_hours=72
        )

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        result = handle_follow_up(task, fake_session, _build_context(fake_session))

        assert result.processed_count == 1
        deal.refresh_from_db()
        assert deal.next_action_at > timezone.now()

        # Confirm claim_due_deal now returns None
        assert claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP") is None

    @patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready")
    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    def test_4_skipped_wait_deals_cannot_be_reclaimed(
        self, mock_capture, mock_agent, mock_summary, mock_ui, fake_session
    ):
        """4. Skipped Deals (agent decided wait) cannot be reclaimed."""
        deal = _make_connected_deal(fake_session, "wait1", message_hours_ago=100)
        mock_agent.return_value = FollowUpDecision(action="wait", follow_up_hours=72)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        result = handle_follow_up(task, fake_session, _build_context(fake_session))

        assert result.skipped_count == 1
        deal.refresh_from_db()
        assert deal.next_action_at > timezone.now()
        assert claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP") is None

    def test_5_when_no_eligible_deals_remain_claim_due_deal_returns_none(self, fake_session):
        """5. When all deals are in cooldown/future, claim_due_deal returns None."""
        deal = _make_connected_deal(fake_session, "in_cool", message_hours_ago=1)
        schedule_next_action(deal, "nudge_cooldown")

        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP")
        assert claimed is None

    def test_6_follow_up_exits_naturally_after_entire_eligible_pool(self, fake_session):
        """6. FOLLOW_UP exits naturally after iterating over the eligible pool."""
        _make_connected_deal(fake_session, "p1", message_hours_ago=1)
        _make_connected_deal(fake_session, "p2", message_hours_ago=2)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        result = handle_follow_up(task, fake_session, _build_context(fake_session))

        assert result.skipped_count == 2
        assert result.error_count == 0

    @patch("outreach_manager.linkedin.tasks.reply.sync_conversation")
    def test_7_reply_executes_immediately_after_follow_up_completes(
        self, mock_sync, fake_session
    ):
        """7. REPLY workflow executes immediately after FOLLOW_UP terminates."""
        mock_result = MagicMock()
        mock_result.new_messages = []
        mock_sync.return_value = mock_result

        _make_connected_deal(fake_session, "seq1", message_hours_ago=1)

        task_f = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        res_f = handle_follow_up(task_f, fake_session, _build_context(fake_session))
        assert res_f.skipped_count == 1

        # Now run REPLY
        task_r = _make_task(Task.TaskType.REPLY_UNREAD, {"campaign_id": fake_session.campaign.pk})
        res_r = handle_reply_unread(task_r, fake_session, _build_context(fake_session))
        assert res_r is not None
        mock_sync.assert_called_once()

    @patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready")
    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin_cli.actions.message.send_raw_message", return_value=True)
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    def test_8_mixed_pool_completes_successfully_without_infinite_loop(
        self, mock_capture, mock_agent, mock_send, mock_summary, mock_ui, fake_session
    ):
        """8. Mixed pool (send + cooldown + wait + exception) completes cleanly."""
        d_cooldown = _make_connected_deal(fake_session, "mix_cool", message_hours_ago=1)
        d_send = _make_connected_deal(fake_session, "mix_send", message_hours_ago=100)
        d_wait = _make_connected_deal(fake_session, "mix_wait", message_hours_ago=100)

        def mock_agent_decisions(session, deal):
            if deal.lead.public_identifier == "mix_send":
                return FollowUpDecision(
                    action="send_message",
                    message="Hello Bob, checking in on our discussion!",
                    follow_up_hours=72,
                )
            if deal.lead.public_identifier == "mix_wait":
                return FollowUpDecision(action="wait", follow_up_hours=72)
            raise RuntimeError("Unexpected deal")

        mock_agent.side_effect = mock_agent_decisions

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        result = handle_follow_up(task, fake_session, _build_context(fake_session))

        assert result.processed_count == 1  # mix_send
        assert result.skipped_count == 2    # mix_cool + mix_wait
        assert result.error_count == 0

        # Entire pool is now exhausted
        assert claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP") is None
