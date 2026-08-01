# outreach_manager/tests/test_production_logging.py
"""Tests for Ticket 8 — Production Logging & Error Classification.

Verifies:
1. Operational events (LLM quota exhausted, browser recovered) produce concise operational logs.
2. Recoverable errors (browser recovery failed) produce concise warnings without stack trace dumps.
3. Unexpected programming bugs produce full tracebacks and create diagnostics packages.
4. Relative diagnostics path is saved and logged.
5. SessionSummary accurately records llm_deferrals, browser_recoveries, and diagnostics_generated.
"""
import logging
from unittest.mock import MagicMock, patch
import pytest

from outreach_manager.core.llm import LLMQuotaExhausted, run_agent_sync
from outreach_manager.core.session_executor import SessionSummary
from outreach_manager.linkedin.browser.session import AccountSession, BrowserRecoveryFailed
from outreach_manager.linkedin.diagnostics import capture_failure


class TestProductionLoggingClassification:
    def test_llm_quota_exhausted_produces_concise_operational_log(self, caplog):
        """Operational event: LLM quota exhaustion logs concise INFO message without stack trace."""
        def quota_coro_fn():
            err = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric")
            err.status_code = 429
            raise err

        with caplog.at_level(logging.INFO):
            with pytest.raises(LLMQuotaExhausted):
                run_agent_sync(quota_coro_fn)

        # Check concise log message presence
        assert any("[INFO] LLM temporarily unavailable." in r.message for r in caplog.records)
        assert any("Provider: Gemini" in r.message or "Reason: Quota exhausted" in r.message for r in caplog.records)
        # Ensure no ERROR or CRITICAL logs or tracebacks were dumped
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        assert not any("Traceback (most recent call last)" in r.message for r in caplog.records)

    def test_browser_recovery_initiated_and_succeeded_logs_concisely(self, caplog):
        """Operational event: Browser recovery logs concise progress messages."""
        profile = MagicMock()
        profile.user = MagicMock()
        profile.linkedin_username = "testuser"
        session = AccountSession(profile)
        session.is_browser_healthy = MagicMock(side_effect=[False, True])
        session.close = MagicMock()

        with caplog.at_level(logging.INFO), patch("outreach_manager.linkedin.browser.launch.start_browser_session"):
            session.ensure_browser()

        assert session.browser_recoveries == 1
        assert any("[WARN] Browser unavailable." in r.message for r in caplog.records)
        assert any("[INFO] Browser recovered successfully" in r.message for r in caplog.records)
        assert not any("Traceback" in r.message for r in caplog.records)

    def test_browser_recovery_failure_logs_concise_warning(self, caplog):
        """Recoverable error: Browser recovery failure produces concise warning without stack dump."""
        profile = MagicMock()
        profile.user = MagicMock()
        profile.linkedin_username = "testuser"
        session = AccountSession(profile)
        session.is_browser_healthy = MagicMock(return_value=False)
        session.close = MagicMock()

        with caplog.at_level(logging.WARNING), patch("outreach_manager.linkedin.browser.launch.start_browser_session"):
            with pytest.raises(BrowserRecoveryFailed):
                session.ensure_browser()

        assert any("[WARN] Browser recovery failed." in r.message for r in caplog.records)
        assert not any("Traceback (most recent call last)" in r.message for r in caplog.records)

    def test_unexpected_exception_creates_diagnostics_package(self, tmp_path, caplog):
        """Unexpected exception: Creates diagnostic package and logs relative folder path."""
        session = MagicMock()
        session.page = MagicMock()
        session.page.is_closed.return_value = False
        session.page.content.return_value = "<html><body>Error page</body></html>"
        session.diagnostics_generated = 0

        err = ValueError("Unexpected database corruption in workflow")

        with patch("outreach_manager.linkedin.diagnostics.DIAGNOSTICS_DIR", tmp_path), caplog.at_level(logging.INFO):
            path_str = capture_failure(session, err)

        assert session.diagnostics_generated == 1
        assert "ValueError" in path_str
        assert (tmp_path / path_str).exists()
        assert (tmp_path / path_str / "error.txt").exists()
        assert "Unexpected database corruption" in (tmp_path / path_str / "error.txt").read_text()
        assert any("Diagnostics saved:" in r.message for r in caplog.records)

    def test_session_summary_tracks_llm_deferrals_and_diagnostics(self, caplog):
        """SessionSummary accurately logs LLM deferrals and diagnostics counts."""
        from datetime import datetime, timezone
        summary = SessionSummary(
            start_time=datetime.now(timezone.utc),
            finish_time=datetime.now(timezone.utc),
            duration_seconds=125.0,
            workflows_executed=["FOLLOW_UP", "REPLY_UNREAD"],
            workflows_skipped=["CONNECT"],
            actions_performed=5,
            deal_errors=1,
            workflow_errors=0,
            fatal_errors=0,
            browser_recoveries=2,
            llm_deferrals=3,
            diagnostics_generated=1,
            errors=["follow_up error for lead-123: invalid state"],
        )

        with caplog.at_level(logging.INFO):
            summary.log_summary()

        output = caplog.text
        assert "Browser Recoveries:    2" in output
        assert "LLM Deferrals:         3" in output
        assert "Diagnostics Generated: 1" in output
        assert "Actions Completed:     5" in output
        assert "Error Details:" in output
        assert "- follow_up error for lead-123: invalid state" in output
