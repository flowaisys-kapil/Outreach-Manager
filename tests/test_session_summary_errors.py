# tests/test_session_summary_errors.py
"""Regression tests for Ticket 5 — Fix Session Summary Error Accounting.

Verifies that SessionSummary accurately reflects deal-level, workflow-level,
and fatal errors from actual execution. All counters must match real events.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import timedelta

from django.utils import timezone

from outreach_manager.core.models import Task, SiteConfig
from outreach_manager.core.session_executor import (
    run_session,
    SessionSummary,
    WorkflowResult,
)


# ---------------------------------------------------------------------------
# Unit: SessionSummary structure
# ---------------------------------------------------------------------------

class TestSessionSummaryStructure:

    def test_clean_session_reports_zero_errors(self):
        """A session with no errors should report zero total_errors."""
        start = timezone.now()
        summary = SessionSummary(
            start_time=start,
            finish_time=start + timedelta(seconds=60),
            duration_seconds=60.0,
            workflows_executed=["connect"],
            actions_performed=3,
        )
        assert summary.deal_errors == 0
        assert summary.workflow_errors == 0
        assert summary.fatal_errors == 0
        assert summary.total_errors == 0
        assert summary.errors == []

    def test_total_errors_sums_all_tiers(self):
        """total_errors property sums deal + workflow + fatal errors."""
        summary = SessionSummary(
            start_time=timezone.now(),
            deal_errors=2,
            workflow_errors=1,
            fatal_errors=0,
            errors=["deal err 1", "deal err 2", "workflow err 1"],
        )
        assert summary.total_errors == 3

    def test_deal_errors_accumulate_independently(self):
        """Deal errors are tracked separately from workflow errors."""
        summary = SessionSummary(start_time=timezone.now())
        summary.deal_errors += 1
        summary.deal_errors += 1
        summary.workflow_errors += 1
        assert summary.deal_errors == 2
        assert summary.workflow_errors == 1
        assert summary.total_errors == 3

    def test_log_summary_runs_without_error(self):
        """log_summary() must not raise even with errors present."""
        start = timezone.now()
        summary = SessionSummary(
            start_time=start,
            finish_time=start + timedelta(seconds=30),
            duration_seconds=30.0,
            deal_errors=1,
            workflow_errors=1,
            errors=["deal err", "workflow err"],
        )
        summary.log_summary()  # Must not raise


# ---------------------------------------------------------------------------
# Unit: WorkflowResult
# ---------------------------------------------------------------------------

class TestWorkflowResult:

    def test_workflow_result_bool_true_when_processed(self):
        result = WorkflowResult(processed_count=3)
        assert bool(result) is True

    def test_workflow_result_bool_false_when_empty(self):
        result = WorkflowResult(processed_count=0)
        assert bool(result) is False

    def test_workflow_result_with_errors_is_not_truthy_if_no_processed(self):
        """Error-only result is falsy (no successful actions)."""
        result = WorkflowResult(processed_count=0, error_count=2)
        assert bool(result) is False

    def test_workflow_result_errors_list_accumulated(self):
        result = WorkflowResult(
            processed_count=1,
            error_count=2,
            errors=["err1", "err2"],
        )
        assert len(result.errors) == 2


# ---------------------------------------------------------------------------
# Integration: run_session deal error propagation
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSessionErrorAccounting:

    def test_deal_level_errors_increment_deal_errors(self, fake_session):
        """WorkflowResult with error_count propagates to summary.deal_errors."""
        def workflow_with_deal_error(task, session, qualifiers):
            return WorkflowResult(
                processed_count=2,
                error_count=1,
                errors=["deal err for profile-123: ValueError"],
            )

        mock_handlers = {Task.TaskType.CONNECT: workflow_with_deal_error}

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert summary.deal_errors == 1
        assert summary.workflow_errors == 0
        assert summary.total_errors == 1
        assert any("deal err for profile-123" in e for e in summary.errors)
        assert summary.actions_performed == 2

    def test_workflow_level_exception_increments_workflow_errors(self, fake_session):
        """An unhandled workflow exception increments workflow_errors, not deal_errors."""
        def crashing_workflow(task, session, qualifiers):
            raise RuntimeError("Browser crashed during CONNECT")

        mock_handlers = {Task.TaskType.CONNECT: crashing_workflow}

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert summary.workflow_errors == 1
        assert summary.deal_errors == 0
        assert summary.total_errors == 1
        assert "CONNECT" in summary.errors[0]

    def test_multiple_errors_accumulate_correctly(self, fake_session):
        """Multiple deal errors across multiple workflows are summed."""
        def workflow_a(task, session, qualifiers):
            return WorkflowResult(processed_count=1, error_count=2, errors=["e1", "e2"])

        def workflow_b(task, session, qualifiers):
            return WorkflowResult(processed_count=0, error_count=1, errors=["e3"])

        mock_handlers = {
            Task.TaskType.CONNECT: workflow_a,
            Task.TaskType.FOLLOW_UP: workflow_b,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert summary.deal_errors == 3
        assert len(summary.errors) == 3

    def test_successful_session_reports_zero_errors(self, fake_session):
        """A fully successful session has zero errors in all categories."""
        def clean_workflow(task, session, qualifiers):
            return WorkflowResult(processed_count=5, error_count=0)

        mock_handlers = {Task.TaskType.CONNECT: clean_workflow}

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert summary.total_errors == 0
        assert summary.deal_errors == 0
        assert summary.workflow_errors == 0
        assert summary.fatal_errors == 0
        assert summary.actions_performed == 5

    def test_successful_actions_counted_despite_errors(self, fake_session):
        """Successful actions are still counted when some deal errors occurred."""
        def mixed_workflow(task, session, qualifiers):
            return WorkflowResult(processed_count=4, error_count=2, errors=["e1", "e2"])

        mock_handlers = {Task.TaskType.CONNECT: mixed_workflow}

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert summary.actions_performed == 4
        assert summary.deal_errors == 2
        assert summary.total_errors == 2

    def test_workflow_continues_after_deal_error(self, fake_session):
        """After a deal-level error in one workflow, subsequent workflows still execute."""
        execution_order = []

        def workflow_with_error(task, session, qualifiers):
            execution_order.append("connect")
            return WorkflowResult(processed_count=1, error_count=1, errors=["deal err"])

        def workflow_after_error(task, session, qualifiers):
            execution_order.append("follow_up")
            return WorkflowResult(processed_count=2, error_count=0)

        mock_handlers = {
            Task.TaskType.CONNECT: workflow_with_error,
            Task.TaskType.FOLLOW_UP: workflow_after_error,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        # Both workflows ran
        assert execution_order == ["connect", "follow_up"]
        # Actions from both counted
        assert summary.actions_performed == 3
        # Only connect's deal error counted
        assert summary.deal_errors == 1
        assert summary.workflow_errors == 0

    def test_workflow_exception_does_not_stop_subsequent_workflows(self, fake_session):
        """An unhandled exception in one workflow does not prevent others from running."""
        execution_order = []

        def crashing_workflow(task, session, qualifiers):
            execution_order.append("connect")
            raise RuntimeError("Fatal crash in connect")

        def surviving_workflow(task, session, qualifiers):
            execution_order.append("follow_up")
            return WorkflowResult(processed_count=3, error_count=0)

        mock_handlers = {
            Task.TaskType.CONNECT: crashing_workflow,
            Task.TaskType.FOLLOW_UP: surviving_workflow,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert execution_order == ["connect", "follow_up"]
        assert summary.workflow_errors == 1
        assert summary.actions_performed == 3

    def test_summary_matches_actual_execution(self, fake_session):
        """End-to-end: summary counters mirror exactly what happened."""
        def workflow_connect(task, session, qualifiers):
            return WorkflowResult(processed_count=3, error_count=1, errors=["connect err"])

        def workflow_follow_up(task, session, qualifiers):
            raise ValueError("follow_up workflow-level error")

        def workflow_reply(task, session, qualifiers):
            return WorkflowResult(processed_count=2, error_count=0)

        mock_handlers = {
            Task.TaskType.CONNECT: workflow_connect,
            Task.TaskType.FOLLOW_UP: workflow_follow_up,
            Task.TaskType.REPLY_UNREAD: workflow_reply,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP, Task.TaskType.REPLY_UNREAD]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        # Exact accounting:
        assert summary.actions_performed == 5    # 3 connect + 2 reply
        assert summary.deal_errors == 1          # 1 from connect
        assert summary.workflow_errors == 1      # 1 from follow_up crash
        assert summary.fatal_errors == 0
        assert summary.total_errors == 2
        assert len(summary.errors) == 2
        assert "connect err" in summary.errors[0]
        assert "follow_up" in summary.errors[1]
