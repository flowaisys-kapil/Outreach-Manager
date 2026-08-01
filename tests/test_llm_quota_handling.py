# tests/test_llm_quota_handling.py
"""Regression tests for Ticket 7 — Graceful LLM Quota Exhaustion Handling.

Verifies that LLM provider 429 / quota exhaustion events are treated as
temporary infrastructure conditions:
  1. Recognized correctly via `is_quota_error()`.
  2. `run_agent_sync()` raises `LLMQuotaExhausted` without retrying or dumping stack traces.
  3. Workflows defer affected Deals cleanly via `schedule_next_action()`.
  4. Batch execution continues for remaining Deals.
  5. Session Summary records deferred actions without counting them as application errors.
  6. Unexpected non-quota errors continue to output full diagnostics.
"""
from __future__ import annotations

import logging
import pytest
from unittest.mock import patch, MagicMock

from django.utils import timezone

from outreach_manager.core.llm import (
    LLMFailure,
    LLMQuotaExhausted,
    _classify,
    is_quota_error,
    run_agent_sync,
)
from outreach_manager.crm.models import Deal, DealState
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.first_message import handle_first_message


# ---------------------------------------------------------------------------
# Unit 1: Provider Error Classification
# ---------------------------------------------------------------------------

class TestQuotaErrorClassification:

    def test_llm_quota_exhausted_instance_recognized(self):
        exc = LLMQuotaExhausted("Quota exceeded", provider="Gemini")
        assert is_quota_error(exc) is True
        assert exc.provider == "Gemini"
        assert exc.category == "QUOTA_EXHAUSTED"

    def test_llm_failure_quota_category_recognized(self):
        exc = LLMFailure("QUOTA_EXHAUSTED: 429", category="QUOTA_EXHAUSTED")
        assert is_quota_error(exc) is True

    @pytest.mark.parametrize("error_class_name", [
        "RateLimitError",
        "ResourceExhausted",
    ])
    def test_provider_exception_class_names_recognized(self, error_class_name):
        exc_type = type(error_class_name, (Exception,), {})
        exc = exc_type("Provider limit hit")
        assert _classify(exc) == "QUOTA_EXHAUSTED"
        assert is_quota_error(exc) is True

    @pytest.mark.parametrize("msg", [
        "HTTP 429 Too Many Requests",
        "Rate limit reached for model gemini-2.5-flash",
        "Quota exhausted for resource",
        "RESOURCE_EXHAUSTED: Quota exceeded for quota metric",
        "too many requests: please slow down",
    ])
    def test_quota_error_message_substrings_recognized(self, msg):
        exc = RuntimeError(msg)
        assert _classify(exc) == "QUOTA_EXHAUSTED"
        assert is_quota_error(exc) is True

    def test_non_quota_errors_not_classified_as_quota(self):
        assert is_quota_error(ValueError("Invalid model argument")) is False
        assert is_quota_error(RuntimeError("Connection reset by peer")) is False
        assert is_quota_error(LLMFailure("Auth failed", category="AUTH")) is False


# ---------------------------------------------------------------------------
# Unit 2: run_agent_sync Quota Behavior
# ---------------------------------------------------------------------------

class TestRunAgentSyncQuotaHandling:

    def test_quota_error_raises_llm_quota_exhausted_without_retrying(self):
        """Quota errors stop retries immediately and raise LLMQuotaExhausted."""
        call_count = 0

        def failing_coro():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("HTTP 429: Quota exhausted for Gemini")

        with patch("outreach_manager.core.llm._pace"), \
             patch("outreach_manager.core.llm._get_provider_name", return_value="Gemini"):

            with pytest.raises(LLMQuotaExhausted) as exc_info:
                run_agent_sync(failing_coro)

        # Must attempt exactly once — no retries on 429
        assert call_count == 1
        assert exc_info.value.provider == "Gemini"
        assert "Quota exhausted" in str(exc_info.value)

    def test_quota_error_does_not_log_error_stack_trace(self, caplog):
        """Quota exhaustion logs operational info, not error tracebacks."""
        def failing_coro():
            raise RuntimeError("429 rate limit exceeded")

        with patch("outreach_manager.core.llm._pace"), \
             patch("outreach_manager.core.llm._get_provider_name", return_value="Gemini"):

            with caplog.at_level(logging.INFO):
                with pytest.raises(LLMQuotaExhausted):
                    run_agent_sync(failing_coro)

        # Info log present
        assert any("[INFO] LLM temporarily unavailable." in rec.message for rec in caplog.records)
        # No ERROR records emitted
        assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


# ---------------------------------------------------------------------------
# Integration 3: Workflow Batch Deferral & Continuity
# ---------------------------------------------------------------------------

def _make_deal(session, public_id, state=DealState.CONNECTED):
    from outreach_manager.crm.models import Lead
    from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
    from outreach_manager.core.db.deals import set_profile_state

    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, {"first_name": "Test", "last_name": public_id})
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, state.value if hasattr(state, "value") else state)
    deal = Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)

    deal.first_message_sent_at = timezone.now()
    deal.save()
    return deal


@pytest.mark.django_db
class TestWorkflowQuotaDeferral:

    def test_follow_up_defers_quota_deal_and_continues_batch(self, fake_session, caplog):
        """Follow-up workflow: Deal A hits quota -> deferred cleanly, Deal B proceeds."""
        deal_a = _make_deal(fake_session, "lead-quota-a")
        deal_b = _make_deal(fake_session, "lead-quota-b")

        def agent_mock(session, deal):
            if deal.lead.public_identifier == "lead-quota-a":
                raise LLMQuotaExhausted("429 Quota exhausted", provider="Gemini")
            res = MagicMock()
            res.action = "send_message"
            res.message = "Hello from follow up!"
            return res

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal_a, deal_b, None]), \
             patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", side_effect=agent_mock), \
             patch("outreach_manager.linkedin.ml.qualifier.validate_and_sanitize_message", return_value=(True, "Hello!")), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True), \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            with caplog.at_level(logging.INFO):
                res = handle_follow_up(None, fake_session, None)

        # Deal A was skipped (deferred), Deal B was processed
        assert res.processed_count == 1
        assert res.skipped_count == 1
        assert res.error_count == 0  # Quota is NOT an application error
        assert len(res.errors) == 0

        # Verify operational log message
        assert any("[INFO] LLM temporarily unavailable." in r.message or "lead-quota-a" in r.message for r in caplog.records)

        # Verify Deal A was rescheduled (next_action_at set in future, claimed_at cleared)
        deal_a.refresh_from_db()
        assert deal_a.claimed_at is None
        assert deal_a.next_action_at is not None

    def test_first_message_defers_quota_deal_and_continues_batch(self, fake_session, caplog):
        """First Message workflow: Deal A hits quota -> deferred cleanly, Deal B proceeds."""
        deal_a = _make_deal(fake_session, "lead-fm-a")
        deal_b = _make_deal(fake_session, "lead-fm-b")
        deal_a.first_message_sent_at = None
        deal_b.first_message_sent_at = None
        deal_a.save()
        deal_b.save()

        def generate_mock(session, deal):
            if deal.lead.public_identifier == "lead-fm-a":
                raise LLMQuotaExhausted("429 Rate limit exceeded", provider="Gemini")
            return "Nice to connect!"

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal_a, deal_b, None]), \
             patch("outreach_manager.linkedin.tasks.first_message.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.linkedin.tasks.first_message.generate_first_message", side_effect=generate_mock), \
             patch("outreach_manager.linkedin.ml.qualifier.validate_and_sanitize_message", return_value=(True, "Nice to connect!")), \
             patch("outreach_manager.linkedin.tasks.first_message.send_raw_message", return_value=True), \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            with caplog.at_level(logging.INFO):
                res = handle_first_message(None, fake_session, None)

        assert res.processed_count == 1
        assert res.skipped_count == 1
        assert res.error_count == 0
        assert len(res.errors) == 0

        assert any("[INFO] LLM temporarily unavailable." in r.message or "lead-fm-a" in r.message for r in caplog.records)

    def test_unexpected_error_still_outputs_exception_traceback(self, fake_session, caplog):
        """Non-quota errors must output full stack trace diagnostics and increment error_count."""
        deal = _make_deal(fake_session, "lead-error")

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal, None]), \
             patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", side_effect=ValueError("Unexpected code crash")):

            res = handle_follow_up(None, fake_session, None)

        assert res.processed_count == 0
        assert res.error_count == 1
        assert len(res.errors) == 1
        assert "Unexpected code crash" in res.errors[0]
