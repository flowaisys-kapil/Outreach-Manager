# outreach_manager/tests/test_deal_resume_and_llm_preservation.py
"""Tests for Ticket 9 — Prevent Wasted LLM Calls & Resume Current Deal.

Verifies:
1. UI validation occurs BEFORE LLM generation (zero LLM calls if UI unavailable).
2. Browser failure before LLM generation -> recovery -> message generated once.
3. Browser failure after LLM generation -> recovery -> same message reused without regenerating.
4. Browser fails twice -> Deal skipped/failed gracefully (no infinite retry loop).
5. Successful browser recovery resumes current Deal exactly once.
"""
from unittest.mock import MagicMock, patch
import pytest

from outreach_manager.crm.models import DealState
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.first_message import handle_first_message
from outreach_manager.linkedin.browser.ui_validation import verify_ui_ready


def _make_deal(session, public_id, state=DealState.CONNECTED):
    from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
    from outreach_manager.core.db.deals import set_profile_state
    from outreach_manager.crm.models import Deal
    from django.utils import timezone

    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, {"first_name": "Test", "last_name": public_id})
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, state.value if hasattr(state, "value") else state)
    deal = Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)

    deal.first_message_sent_at = timezone.now()
    deal.save()
    return deal


@pytest.mark.django_db
class TestTicket9PreventWastedLLMAndResumeDeal:

    def test_llm_never_called_when_ui_unavailable(self, fake_session):
        """Part 1: If UI/browser verification fails, LLM agent is NEVER invoked."""
        deal = _make_deal(fake_session, "lead-ui-fail")
        agent_mock = MagicMock()

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal, None]), \
             patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready", side_effect=RuntimeError("UI Unavailable")), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", agent_mock):

            res = handle_follow_up(None, fake_session, None)

        assert agent_mock.call_count == 0, "LLM agent must NOT be called when UI validation fails"
        assert res.error_count == 1 or res.skipped_count == 1

    def test_browser_failure_before_llm_generates_message_once_after_recovery(self, fake_session):
        """Part 1 & 2: Browser failure before LLM -> recovery -> LLM called ONCE."""
        deal = _make_deal(fake_session, "lead-recover-before")
        agent_calls = []

        def agent_mock(session, deal):
            agent_calls.append(deal.lead.public_identifier)
            res = MagicMock()
            res.action = "send_message"
            res.message = "Hello after browser recovery!"
            return res

        # UI readiness fails on 1st call, succeeds on 2nd call (post-recovery)
        ui_readiness_side_effects = [RuntimeError("Browser crashed during UI nav"), True]

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal, None]), \
             patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready", side_effect=ui_readiness_side_effects), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", side_effect=agent_mock), \
             patch("outreach_manager.linkedin.ml.qualifier.validate_and_sanitize_message", return_value=(True, "Hello!")), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True), \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            res = handle_follow_up(None, fake_session, None)

        assert res.processed_count == 1
        assert len(agent_calls) == 1, "LLM agent must be called exactly ONCE"
        assert agent_calls[0] == "lead-recover-before"

    def test_browser_failure_after_llm_preserves_generated_message(self, fake_session):
        """Part 2 & 4: Browser fails during send -> recovery -> SAME generated message reused."""
        deal = _make_deal(fake_session, "lead-recover-after")
        agent_calls = []

        def agent_mock(session, deal):
            agent_calls.append(deal.lead.public_identifier)
            res = MagicMock()
            res.action = "send_message"
            res.message = "Preserved message content"
            return res

        # send_raw_message fails on 1st call, succeeds on 2nd call (post-recovery)
        send_side_effects = [RuntimeError("Browser disconnected during send"), True]

        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal, None]), \
             patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready", return_value=True), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", side_effect=agent_mock), \
             patch("outreach_manager.linkedin.ml.qualifier.validate_and_sanitize_message", return_value=(True, "Preserved message content")), \
             patch("linkedin_cli.actions.message.send_raw_message", side_effect=send_side_effects) as send_mock, \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            res = handle_follow_up(None, fake_session, None)

        assert res.processed_count == 1
        assert len(agent_calls) == 1, "LLM must NOT be called a second time when message is preserved"
        assert send_mock.call_count == 2
        # Verify 2nd send call reused the exact same message
        assert send_mock.call_args_list[1][0][2] == "Preserved message content"

    def test_browser_fails_twice_skips_deal_gracefully(self, fake_session):
        """Part 3: Browser fails twice on same Deal -> Deal skipped/failed, batch continues."""
        deal_a = _make_deal(fake_session, "lead-fail-twice")
        deal_b = _make_deal(fake_session, "lead-succeed")

        agent_res = MagicMock(action="send_message", message="Hello!")
        with patch("outreach_manager.linkedin.scheduler.claim_due_deal", side_effect=[deal_a, deal_b, None]), \
             patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False), \
             patch("outreach_manager.linkedin.tasks.follow_up.verify_ui_ready", side_effect=[RuntimeError("Err 1"), RuntimeError("Err 2"), True]), \
             patch("outreach_manager.core.db.deals.capture_and_contribute"), \
             patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("outreach_manager.core.agents.follow_up.run_follow_up_agent", return_value=agent_res) as agent_mock, \
             patch("outreach_manager.linkedin.ml.qualifier.validate_and_sanitize_message", return_value=(True, "Hello!")), \
             patch("linkedin_cli.actions.message.send_raw_message", return_value=True), \
             patch("outreach_manager.linkedin.db.chat.sync_conversation"):

            res = handle_follow_up(None, fake_session, None)

        # Deal A failed after 1 retry attempt, Deal B succeeded
        assert res.processed_count == 1
        assert res.error_count == 1
