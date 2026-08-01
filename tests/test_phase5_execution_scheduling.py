# tests/test_phase5_execution_scheduling.py
"""Phase 5 regression tests — Execution Scheduling & Workflow Reliability.

Covers:
  1. CONNECTED transition -> FIRST_MESSAGE scheduled due now
  2. FOLLOW_UP sent -> Next follow-up scheduled after unanswered_count * 3 days
  3. WAIT decision -> Correct future scheduling
  4. COMPLETE state -> next_action_at is cleared (None)
  5. Task failure -> Retry scheduled (now + 1h) and claim cleared
  6. No work due -> run_daemon(exit_on_empty=True) exits without browser launch
  7. Duplicate execution protection -> claim_due_deal prevents concurrent execution
  8. State transitions remain unchanged (DealState FSM preserved)
"""
import os
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.linkedin.scheduler import (
    claim_due_deal,
    earliest_due_time,
    has_due_work,
    release_claim,
    schedule_next_action,
)

SAMPLE_PROFILE = {
    "first_name": "Scheduled",
    "last_name": "Lead",
    "headline": "CEO at Acme",
    "positions": [{"company_name": "Acme Inc"}],
}


def _make_qualified(session, public_id):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)


@pytest.mark.django_db
class TestPhase5ExecutionScheduling:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_1_connected_schedules_first_message(self, fake_session):
        """1. CONNECTED -> FIRST_MESSAGE scheduled (next_action_at <= now)."""
        deal = _make_qualified(fake_session, "lead1")
        set_profile_state(fake_session, "lead1", DealState.CONNECTED.value)
        deal.refresh_from_db()

        assert deal.state == DealState.CONNECTED
        assert deal.next_action_at is not None
        assert deal.next_action_at <= timezone.now() + timedelta(seconds=5)

    def test_2_followup_sent_schedules_next_followup(self, fake_session):
        """2. FOLLOW_UP sent -> Next follow-up scheduled after unanswered_count * 3 days."""
        deal = _make_qualified(fake_session, "lead2")
        set_profile_state(fake_session, "lead2", DealState.CONNECTED.value)
        deal.refresh_from_db()
        deal.first_message_sent_at = timezone.now() - timedelta(days=1)
        deal.save()

        # Simulate follow-up message sent
        next_t = schedule_next_action(deal, "send_message")
        deal.refresh_from_db()

        assert next_t is not None
        # 1 unanswered nudge -> ~3 days in future
        expected = timezone.now() + timedelta(days=3)
        assert abs((deal.next_action_at - expected).total_seconds()) < 60

    def test_3_wait_decision_schedules_future_check(self, fake_session):
        """3. WAIT decision -> Schedules future next_action_at."""
        deal = _make_qualified(fake_session, "lead3")
        set_profile_state(fake_session, "lead3", DealState.CONNECTED.value)
        deal.refresh_from_db()
        deal.first_message_sent_at = timezone.now() - timedelta(days=1)
        deal.save()

        next_t = schedule_next_action(deal, "wait", delay_hours=48.0)
        deal.refresh_from_db()

        assert next_t is not None
        expected = timezone.now() + timedelta(hours=48.0)
        assert abs((deal.next_action_at - expected).total_seconds()) < 60

    def test_4_complete_clears_next_action(self, fake_session):
        """4. COMPLETE -> next_action_at is set to None (no future work)."""
        deal = _make_qualified(fake_session, "lead4")
        set_profile_state(fake_session, "lead4", DealState.CONNECTED.value)
        set_profile_state(fake_session, "lead4", DealState.COMPLETED.value)
        deal.refresh_from_db()

        assert deal.state == DealState.COMPLETED
        assert deal.next_action_at is None

    def test_5_task_failure_schedules_retry_and_clears_claim(self, fake_session):
        """5. Task failure -> Retry scheduled (now + 1h) and claim cleared."""
        deal = _make_qualified(fake_session, "lead5")
        set_profile_state(fake_session, "lead5", DealState.CONNECTED.value)
        # Set first_message_sent_at so the FOLLOW_UP workflow-aware filter passes
        Deal.objects.filter(lead__public_identifier="lead5").update(
            first_message_sent_at=timezone.now() - timedelta(hours=24)
        )

        # Claim the deal
        claimed = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "FOLLOW_UP")
        assert claimed is not None
        assert claimed.claimed_at is not None

        # Simulate execution failure
        next_t = schedule_next_action(claimed, "failed")
        claimed.refresh_from_db()

        assert claimed.claimed_at is None
        assert claimed.next_action_at is not None
        expected = timezone.now() + timedelta(hours=1)
        assert abs((claimed.next_action_at - expected).total_seconds()) < 60

    def test_6_no_work_due_exits_daemon_without_browser_launch(self, fake_session):
        """6. No work due -> run_daemon(exit_on_empty=True) exits without browser launch."""
        from outreach_manager.core.daemon import run_daemon

        # Ensure no due work exists for campaign
        Deal.objects.filter(campaign=fake_session.campaign).update(
            next_action_at=timezone.now() + timedelta(days=5),
        )

        with patch("linkedin_cli.api.client.PlaywrightLinkedinAPI") as mock_browser:
            run_daemon(fake_session, exit_on_empty=True)

        # Browser API must NOT be initialized when no work is due!
        mock_browser.assert_not_called()

    def test_7_claim_due_deal_prevents_duplicate_execution(self, fake_session):
        """7. Duplicate execution protection -> claim_due_deal locks out concurrent worker."""
        deal = _make_qualified(fake_session, "lead7")
        set_profile_state(fake_session, "lead7", DealState.CONNECTED.value)

        # First worker claims the deal
        worker1_claim = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "WORKER_1")
        assert worker1_claim is not None
        assert worker1_claim.pk == deal.pk

        # Second worker attempts to claim the same deal -> gets None
        worker2_claim = claim_due_deal(fake_session.campaign, [DealState.CONNECTED], "WORKER_2")
        assert worker2_claim is None

    def test_8_state_transitions_remain_unchanged(self, fake_session):
        """8. Deal state FSM transitions are preserved."""
        deal = _make_qualified(fake_session, "lead8")
        assert deal.state == DealState.QUALIFIED

        set_profile_state(fake_session, "lead8", DealState.READY_TO_CONNECT.value)
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_CONNECT

        set_profile_state(fake_session, "lead8", DealState.PENDING.value)
        deal.refresh_from_db()
        assert deal.state == DealState.PENDING

        set_profile_state(fake_session, "lead8", DealState.CONNECTED.value)
        deal.refresh_from_db()
        assert deal.state == DealState.CONNECTED
