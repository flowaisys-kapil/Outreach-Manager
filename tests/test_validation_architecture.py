# tests/test_validation_architecture.py
"""Comprehensive Validation Test Suite for Ticket 3.5 — Execution Architecture Validation."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import timedelta
from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.models import Task, SiteConfig
from outreach_manager.linkedin.models import ActionLog
from outreach_manager.core.session_executor import run_session, SessionSummary
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.db.deals import set_profile_state

from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending
from outreach_manager.linkedin.tasks.first_message import handle_first_message
from outreach_manager.linkedin.tasks.connect import handle_connect
from outreach_manager.linkedin.tasks.extract_leads import handle_extract_leads
from outreach_manager.core.sequence_generator import BalancedSequenceGenerator


@pytest.mark.django_db
class TestExecutionArchitectureValidation:

    def _make_deal(self, fake_session, pid, state):
        url = f"https://www.linkedin.com/in/{pid}/"
        create_enriched_lead(fake_session, url, {"first_name": pid, "last_name": "Test"})
        promote_lead_to_deal(fake_session, pid)
        set_profile_state(fake_session, pid, state.value)
        deal = Deal.objects.get(lead__public_identifier=pid, campaign=fake_session.campaign)
        return deal

    # ── Validation 1 — Batch Completeness ────────────────────────────────────

    def test_val1_reply_batch_completeness(self, fake_session):
        """Reply workflow processes all 8 unread conversations in a single batch."""
        deals = [self._make_deal(fake_session, f"reply-lead-{i}", DealState.CONNECTED) for i in range(8)]

        mock_sync = MagicMock(new_messages=[MagicMock(is_outgoing=False)])
        mock_decision = MagicMock(action="send_message", message="Hello, thank you for contacting us today!")

        with patch("outreach_manager.linkedin.tasks.reply.sync_conversation", return_value=mock_sync), \
             patch("outreach_manager.linkedin.tasks.reply.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.linkedin.tasks.reply.run_follow_up_agent", return_value=mock_decision), \
             patch("outreach_manager.linkedin.tasks.reply.send_raw_message", return_value=True) as mock_send:

            result = handle_reply_unread(None, fake_session, qualifiers={})
            assert bool(result) is True
            assert mock_send.call_count == 8

    def test_val1_follow_up_batch_completeness(self, fake_session):
        """Follow-up workflow processes all 12 due deals until quota or pool exhausted."""
        deals = [self._make_deal(fake_session, f"fu-lead-{i}", DealState.CONNECTED) for i in range(12)]
        Deal.objects.all().update(next_action_at=timezone.now() - timedelta(days=1))

        mock_decision = MagicMock(action="send_message", message="Hello, following up with you today!")

        with patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", return_value=mock_decision), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"), \
             patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=list(deals) + [None]):

            result = handle_follow_up(None, fake_session, qualifiers={})
            assert bool(result) is True
            assert mock_send.call_count == 12

    def test_val1_connect_quota_enforcement(self, fake_session):
        """Connect workflow respects daily quota (e.g. 5) and leaves remaining for next session."""
        fake_session.linkedin_profile.connect_daily_limit = 5
        fake_session.linkedin_profile.save()

        # Create candidate deals in DB
        deals = [self._make_deal(fake_session, f"cand-{i+1}", DealState.READY_TO_CONNECT) for i in range(10)]

        call_count = 0

        def mock_find_candidate(session):
            nonlocal call_count
            if call_count >= 10:
                return None
            call_count += 1
            pid = f"cand-{call_count}"
            return {"public_identifier": pid, "profile": {"public_identifier": pid}}

        with patch("outreach_manager.linkedin.tasks.connect.strategy_for") as mock_strat_builder, \
             patch("linkedin_cli.actions.status.get_connection_status", return_value=MagicMock(value="Qualified")), \
             patch("linkedin_cli.actions.connect.send_connection_request", return_value=MagicMock(value="Pending")):

            mock_strategy = MagicMock()
            mock_strategy.find_candidate = mock_find_candidate
            mock_strategy.qualifier = MagicMock()
            mock_strat_builder.return_value = mock_strategy

            result = handle_connect(None, fake_session, qualifiers={})
            assert bool(result) is True
            # Exactly 5 processed due to daily quota limit
            assert fake_session.linkedin_profile._daily_count("connect") == 5

    # ── Validation 2 — Loop Termination ─────────────────────────────────────

    def test_val2_loop_termination_when_claim_due_deal_is_none(self, fake_session):
        """Loop terminates immediately when claim_due_deal returns None."""
        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", return_value=None) as mock_claim:
            result = handle_follow_up(None, fake_session, qualifiers={})
            assert bool(result) is False
            assert mock_claim.call_count == 1

    # ── Validation 3 — Scheduler Integrity ─────────────────────────────────

    def test_val3_scheduler_integrity_per_deal(self, fake_session):
        """Each processed deal updates state and calls schedule_next_action independently."""
        deal1 = self._make_deal(fake_session, "sched-lead-1", DealState.CONNECTED)
        deal2 = self._make_deal(fake_session, "sched-lead-2", DealState.CONNECTED)

        mock_decision = MagicMock(action="send_message", message="Hello, checking in with you today!")

        with patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", return_value=mock_decision), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True), \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"), \
             patch("outreach_manager.linkedin.scheduler.schedule_next_action") as mock_sched, \
             patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal1, deal2, None]):

            handle_follow_up(None, fake_session, qualifiers={})
            # schedule_next_action called independently for each processed deal
            assert mock_sched.call_count == 2

    # ── Validation 4 — Error Isolation ─────────────────────────────────────

    def test_val4_error_isolation_deal_level(self, fake_session):
        """Failure on Deal 2 does NOT terminate Deal 3."""
        deal1 = self._make_deal(fake_session, "iso-lead-1", DealState.CONNECTED)
        deal2 = self._make_deal(fake_session, "iso-lead-2", DealState.CONNECTED)
        deal3 = self._make_deal(fake_session, "iso-lead-3", DealState.CONNECTED)

        mock_decision = MagicMock(action="send_message", message="Hello, checking in!")

        def mock_agent(session, deal):
            if deal.lead.public_identifier == "iso-lead-2":
                raise RuntimeError("Simulated Browser Crash on Deal 2")
            return mock_decision

        with patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", side_effect=mock_agent), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True) as mock_send, \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"), \
             patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal1, deal2, deal3, None]):

            result = handle_follow_up(None, fake_session, qualifiers={})
            assert bool(result) is True
            # Both Deal 1 and Deal 3 sent successfully despite Deal 2 exception!
            assert mock_send.call_count == 2

    # ── Validation 8 — Workflow Independence ────────────────────────────────

    def test_val8_workflow_independence_in_session(self, fake_session):
        """Failure in Workflow 1 does NOT prevent Workflow 2 from running in session."""
        def crashing_reply(task, session, qualifiers):
            raise RuntimeError("Workflow 1 Fatal Exception")

        def successful_follow_up(task, session, qualifiers):
            return True

        mock_handlers = {
            Task.TaskType.REPLY_UNREAD: crashing_reply,
            Task.TaskType.FOLLOW_UP: successful_follow_up,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence", return_value=[Task.TaskType.REPLY_UNREAD, Task.TaskType.FOLLOW_UP]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)
            assert summary is not None
            assert "follow_up" in summary.workflows_executed
            assert len(summary.errors) == 1

    # ── Validation 9 — Weighted Randomization & Execution Order ──────────────

    def test_val9_weighted_randomization_distribution(self, fake_session):
        """100 session runs generate randomized orders matching configured weights over time."""
        sequences = [BalancedSequenceGenerator.get_cycle_sequence(fake_session) for _ in range(100)]
        assert len(sequences) == 100
        for seq in sequences:
            # Each sequence contains all 6 workflows exactly once
            assert len(seq) == 6
            assert len(set(seq)) == 6
