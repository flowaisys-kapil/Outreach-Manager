# tests/test_browser_recovery.py
"""Regression tests for Ticket 6 — Robust Browser Recovery & Session Continuity.

Covers:
  1. Page closes unexpectedly → recovery attempted.
  2. Context closes unexpectedly → recovery attempted.
  3. Browser process exits → recovery attempted.
  4. Recovery succeeds → workflow continues.
  5. Recovery fails once → workflow exits gracefully, session continues.
  6. No infinite recovery loop (re-entrant guard).
  7. Session summary correctly records browser recovery / failure.
  8. reset_recovery_state() allows recovery in subsequent workflow.

All tests are pure unit tests (no Playwright, no DB required).
is_browser_healthy() and ensure_browser() use MagicMock Playwright objects.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call

from outreach_manager.linkedin.browser.exceptions import BrowserRecoveryFailed
from outreach_manager.linkedin.browser.session import AccountSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_session(page_closed=False, context_closed=False, browser_connected=True, has_playwright=True):
    """Build a minimal AccountSession with mocked Playwright objects."""
    profile = MagicMock()
    profile.pk = 1
    profile.linkedin_username = "test_user"
    profile.cookie_data = None

    session = AccountSession(profile)

    if has_playwright:
        session.playwright = MagicMock()
        session.browser = MagicMock()
        session.browser.is_connected.return_value = browser_connected
        session.context = MagicMock()
        session.context.is_closed.return_value = context_closed
        session.page = MagicMock()
        session.page.is_closed.return_value = page_closed
    else:
        session.playwright = None
        session.browser = None
        session.context = None
        session.page = None

    return session


# ---------------------------------------------------------------------------
# 1-3: is_browser_healthy() — individual failure modes
# ---------------------------------------------------------------------------

class TestIsBrowserHealthy:

    def test_all_healthy_returns_true(self):
        session = make_session()
        assert session.is_browser_healthy() is True

    def test_no_playwright_objects_returns_false(self):
        session = make_session(has_playwright=False)
        assert session.is_browser_healthy() is False

    def test_page_closed_returns_false(self):
        """Page closed — unhealthy regardless of context/browser state."""
        session = make_session(page_closed=True)
        assert session.is_browser_healthy() is False

    def test_context_closed_returns_false(self):
        """Context closed — unhealthy even if page appears open."""
        session = make_session(context_closed=True)
        assert session.is_browser_healthy() is False

    def test_browser_disconnected_returns_false(self):
        """Browser process exited — unhealthy."""
        session = make_session(browser_connected=False)
        assert session.is_browser_healthy() is False

    def test_playwright_internal_exception_returns_false(self):
        """Any Playwright internal error → treat as dead."""
        session = make_session()
        session.page.is_closed.side_effect = RuntimeError("Playwright internal error")
        assert session.is_browser_healthy() is False


# ---------------------------------------------------------------------------
# 4: Recovery succeeds — workflow continues
# ---------------------------------------------------------------------------

class TestBrowserRecoverySuccess:

    def test_unhealthy_browser_triggers_recovery(self):
        """ensure_browser() calls start_browser_session when browser is unhealthy."""
        session = make_session(page_closed=True)

        def fake_start(session):
            # Simulate successful rebuild: attach fresh healthy objects
            session.playwright = MagicMock()
            session.browser = MagicMock()
            session.browser.is_connected.return_value = True
            session.context = MagicMock()
            session.context.is_closed.return_value = False
            session.page = MagicMock()
            session.page.is_closed.return_value = False

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session",
                   side_effect=fake_start) as mock_start:
            session.ensure_browser()

        mock_start.assert_called_once_with(session=session)
        assert session.is_browser_healthy() is True
        # Recovery flags reset after success
        assert session._recovery_in_progress is False

    def test_healthy_browser_skips_recovery(self):
        """ensure_browser() does NOT call start_browser_session when browser is healthy."""
        session = make_session()

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session") as mock_start, \
             patch.object(session, "_maybe_refresh_cookies"):
            session.ensure_browser()

        # The key assertion: no browser rebuild was attempted
        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# 5: Recovery fails once — workflow exits gracefully
# ---------------------------------------------------------------------------

class TestBrowserRecoveryFailure:

    def test_recovery_failure_raises_browser_recovery_failed(self):
        """When start_browser_session() raises, ensure_browser raises BrowserRecoveryFailed."""
        session = make_session(page_closed=True)

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session",
                   side_effect=RuntimeError("CDP not reachable")):
            with pytest.raises(BrowserRecoveryFailed) as exc_info:
                session.ensure_browser()

        assert "CDP not reachable" in str(exc_info.value)

    def test_recovery_failure_leaves_objects_cleaned_up(self):
        """After a failed recovery, all Playwright objects must be None."""
        session = make_session(page_closed=True)

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session",
                   side_effect=RuntimeError("launch failed")):
            with pytest.raises(BrowserRecoveryFailed):
                session.ensure_browser()

        # close() should have been called — everything is None
        assert session.page is None
        assert session.context is None
        assert session.browser is None
        assert session.playwright is None

    def test_recovery_failure_clears_recovery_in_progress_flag(self):
        """_recovery_in_progress must be False after a failed attempt."""
        session = make_session(page_closed=True)

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session",
                   side_effect=RuntimeError("broken")):
            with pytest.raises(BrowserRecoveryFailed):
                session.ensure_browser()

        assert session._recovery_in_progress is False


# ---------------------------------------------------------------------------
# 6: No infinite recovery loop
# ---------------------------------------------------------------------------

class TestNoInfiniteRecoveryLoop:

    def test_re_entrant_recovery_raises_immediately(self):
        """If ensure_browser() is called while recovery is in progress, raise immediately."""
        session = make_session(page_closed=True)
        # Simulate re-entrant call by pre-setting the guard
        session._recovery_in_progress = True

        with pytest.raises(BrowserRecoveryFailed, match="Re-entrant"):
            session.ensure_browser()

    def test_second_failure_in_same_workflow_does_not_retry(self):
        """After one recovery attempt (failed), subsequent ensure_browser() calls
        raise without attempting start_browser_session() again."""
        session = make_session(page_closed=True)

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session",
                   side_effect=RuntimeError("broken")) as mock_start:
            # First call attempts recovery and fails
            with pytest.raises(BrowserRecoveryFailed):
                session.ensure_browser()
            assert mock_start.call_count == 1

            # Manually make page closed again (simulating continued stale state)
            session.page = MagicMock()
            session.page.is_closed.return_value = True

            # Second call should NOT invoke start_browser_session again
            with pytest.raises(BrowserRecoveryFailed, match="already failed"):
                session.ensure_browser()
            assert mock_start.call_count == 1  # Still exactly 1, not 2

    def test_reset_recovery_state_allows_fresh_recovery(self):
        """reset_recovery_state() clears the 'already attempted' guard.
        This is called by the Session Executor between workflows.
        """
        session = make_session(page_closed=True)

        with patch("outreach_manager.linkedin.browser.launch.start_browser_session",
                   side_effect=RuntimeError("broken")):
            with pytest.raises(BrowserRecoveryFailed):
                session.ensure_browser()

        assert session._recovery_attempted is True

        # Session Executor resets state before the next workflow
        session.reset_recovery_state()
        assert session._recovery_attempted is False
        assert session._recovery_in_progress is False


# ---------------------------------------------------------------------------
# 7: Session summary correctly records browser recovery/failure
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSessionSummaryBrowserAccounting:

    def test_browser_recovery_failed_recorded_as_workflow_error(self, fake_session):
        """BrowserRecoveryFailed is counted as workflow_errors, not fatal_errors."""
        from outreach_manager.core.session_executor import run_session
        from outreach_manager.core.models import Task

        def crashing_workflow(task, session, qualifiers):
            raise BrowserRecoveryFailed("Browser recovery failed for test: CDP not reachable")

        mock_handlers = {Task.TaskType.CONNECT: crashing_workflow}

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_session(fake_session, exit_on_empty=True)

        assert summary.workflow_errors == 1
        assert summary.fatal_errors == 0
        assert summary.deal_errors == 0
        assert summary.total_errors == 1
        assert any("browser recovery failed" in e.lower() for e in summary.errors)

    def test_browser_recovery_failure_does_not_stop_subsequent_workflows(self, fake_session):
        """After a BrowserRecoveryFailed in workflow A, workflow B still runs."""
        from outreach_manager.core.session_executor import run_session
        from outreach_manager.core.models import Task
        from outreach_manager.core.workflow_result import WorkflowResult

        execution_order = []

        def workflow_with_browser_failure(task, session, qualifiers):
            execution_order.append("connect")
            raise BrowserRecoveryFailed("Browser died in connect")

        def workflow_after_failure(task, session, qualifiers):
            execution_order.append("follow_up")
            return WorkflowResult(processed_count=3, error_count=0)

        mock_handlers = {
            Task.TaskType.CONNECT: workflow_with_browser_failure,
            Task.TaskType.FOLLOW_UP: workflow_after_failure,
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
        assert summary.workflow_errors == 1
        assert summary.actions_performed == 3
        assert "connect" in summary.workflows_skipped

    def test_reset_recovery_called_before_each_workflow(self, fake_session):
        """Session Executor calls reset_recovery_state() before each workflow."""
        from outreach_manager.core.session_executor import run_session
        from outreach_manager.core.models import Task
        from outreach_manager.core.workflow_result import WorkflowResult

        reset_calls = []

        original_reset = fake_session.reset_recovery_state

        def tracking_reset():
            reset_calls.append(1)
            original_reset()

        fake_session.reset_recovery_state = tracking_reset

        def clean_workflow(task, session, qualifiers):
            return WorkflowResult(processed_count=1)

        mock_handlers = {
            Task.TaskType.CONNECT: clean_workflow,
            Task.TaskType.FOLLOW_UP: clean_workflow,
        }

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", mock_handlers), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence",
                   return_value=[Task.TaskType.CONNECT, Task.TaskType.FOLLOW_UP]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            run_session(fake_session, exit_on_empty=True)

        # reset should have been called once per workflow (2 workflows)
        assert len(reset_calls) == 2
