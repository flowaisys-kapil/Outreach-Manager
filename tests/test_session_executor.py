# tests/test_session_executor.py
"""Unit tests for Ticket 2 — Session Executor."""
import pytest
from unittest.mock import patch, MagicMock
from django.utils import timezone
from datetime import timedelta

from outreach_manager.core.models import Task, SiteConfig
from outreach_manager.core.session_executor import (
    run_session,
    SessionSummary,
    seconds_until_active,
)
from outreach_manager.crm.models import Deal, DealState
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal


@pytest.mark.django_db
class TestSessionExecutor:

    def test_session_summary_structure(self):
        start = timezone.now()
        finish = start + timedelta(seconds=45)
        summary = SessionSummary(
            start_time=start,
            finish_time=finish,
            duration_seconds=45.0,
            workflows_executed=["CONNECT", "FOLLOW_UP"],
            workflows_skipped=["REPLY_UNREAD"],
            actions_performed=2,
            errors=[],
        )
        assert summary.duration_seconds == 45.0
        assert summary.actions_performed == 2
        assert len(summary.workflows_executed) == 2
        assert len(summary.workflows_skipped) == 1
        summary.log_summary()  # Verify logging runs without error

    def test_run_session_no_due_work_exits_without_browser(self, fake_session):
        """When no work is due, session exits cleanly without browser initialization."""
        Deal.objects.filter(campaign=fake_session.campaign).update(
            next_action_at=timezone.now() + timedelta(days=5),
        )
        with patch("linkedin_cli.api.client.PlaywrightLinkedinAPI") as mock_browser:
            summary = run_session(fake_session, exit_on_empty=True)
            mock_browser.assert_not_called()
            assert summary is not None
            assert summary.actions_performed == 0

    def test_run_session_single_weighted_randomization(self, fake_session):
        """Session generates weighted sequence ONCE and runs workflows directly."""
        executed_workflows = []

        def mock_handler(task, session, qualifiers):
            nonlocal executed_workflows
            # Assert task is None (SyntheticTask removed!)
            assert task is None
            return True

        mock_handlers = {
            Task.TaskType.CONNECT: mock_handler,
            Task.TaskType.FOLLOW_UP: mock_handler,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence", return_value=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP]) as mock_gen, \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

            # Assert sequence was generated exactly ONCE per session
            assert mock_gen.call_count == 1
            assert summary.workflows_executed == ["connect", "follow_up"]
            assert summary.actions_performed == 2
            assert len(summary.errors) == 0

    def test_task_override_in_session(self, fake_session):
        """Task override executes only the specified workflow."""
        site_config = SiteConfig.load()
        site_config.simulated_task = "connect"
        site_config.override_expires_at = timezone.now() + timedelta(minutes=10)
        site_config.save()

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", {Task.TaskType.CONNECT: lambda t, s, q: True}), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)
            assert summary.workflows_executed == ["connect"]
            assert len(summary.workflows_skipped) == 0
