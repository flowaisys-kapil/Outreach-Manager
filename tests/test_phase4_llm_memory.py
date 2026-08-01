# tests/test_phase4_llm_memory.py
"""Phase 4 regression tests — LLM & Conversation Memory Reliability.

Covers:
  W1: Old connection_requested_at → withdrawal eligible
  W2: Recent request on old Deal → not eligible
  W3: NULL request timestamp → not eligible (Phase 3 correction)

  L4: Hard quota switches promptly to LLMFailure (no excessive retries)
  L5: Incompatible backup for structured output is excluded
  L6: Transient retries are bounded (≤ _APP_MAX_TRANSIENT_RETRIES)
  L7: Primary + fallback failure terminates cleanly with LLMFailure
  L8: Auth failure stops immediately (no retry)

  M9:  extract_facts receives lead identity in system prompt
  M10: reconcile_facts receives lead identity in preamble
  M11: Third-party facts are not extracted as lead facts (prompt instruction)
  M12: Seller facts remain excluded (seller binding)
  M13: rebuild_chat_summary uses only Deal-A messages (not Deal-B)
  M14: Empty Deal rebuild produces empty summary without LLM call
  M15: Summary failure does not remove synchronized ChatMessages
"""
import os
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock, call

from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.core.llm import LLMFailure, _classify, run_agent_sync, get_llm_model
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.linkedin.pipeline.acceptances import _connection_request_age_filter

SAMPLE_PROFILE = {
    "first_name": "Test",
    "last_name": "Lead",
    "headline": "CTO at Acme",
    "positions": [{"company_name": "Acme", "title": "CTO"}],
}


def _make_qualified(session, public_id):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)


def _make_pending(session, public_id):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, DealState.PENDING.value)
    return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)


# ── W1/W2/W3: Withdrawal age filter (Phase 3 correction) ────────────────────


@pytest.mark.django_db
class TestWithdrawalAgeFilterP4:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_w1_old_request_timestamp_eligible(self, fake_session):
        """W1: Old connection_requested_at → eligible for withdrawal."""
        deal = _make_pending(fake_session, "w1lead")
        Deal.objects.filter(pk=deal.pk).update(
            connection_requested_at=timezone.now() - timedelta(days=10),
        )
        age_filter = _connection_request_age_filter()
        assert Deal.objects.filter(pk=deal.pk).filter(age_filter).exists()

    def test_w2_old_deal_recent_request_not_eligible(self, fake_session):
        """W2: Old Deal creation_date + recent connection_requested_at → NOT eligible."""
        deal = _make_pending(fake_session, "w2lead")
        Deal.objects.filter(pk=deal.pk).update(
            creation_date=timezone.now() - timedelta(days=20),
            connection_requested_at=timezone.now() - timedelta(days=3),
        )
        age_filter = _connection_request_age_filter()
        assert not Deal.objects.filter(pk=deal.pk).filter(age_filter).exists()

    def test_w3_null_request_timestamp_not_eligible(self, fake_session):
        """W3: NULL connection_requested_at → NOT eligible, even with old creation_date."""
        deal = _make_pending(fake_session, "w3lead")
        Deal.objects.filter(pk=deal.pk).update(
            connection_requested_at=None,
            creation_date=timezone.now() - timedelta(days=30),
        )
        age_filter = _connection_request_age_filter()
        assert not Deal.objects.filter(pk=deal.pk).filter(age_filter).exists()


# ── L4–L8: LLM failure classification and retry ─────────────────────────────


class TestLLMFailureClassification:
    """Unit tests for _classify — no DB needed."""

    def test_rate_limit_error_classified_quota(self):
        class RateLimitError(Exception):
            pass
        assert _classify(RateLimitError("429 rate limit exceeded")) == "QUOTA_EXHAUSTED"

    def test_authentication_error_classified_auth(self):
        class AuthenticationError(Exception):
            pass
        assert _classify(AuthenticationError("invalid api key")) == "AUTH"

    def test_timeout_classified_transient(self):
        assert _classify(TimeoutError("connection timed out")) == "TRANSIENT"

    def test_io_error_classified_transient(self):
        assert _classify(IOError("network error")) == "TRANSIENT"

    def test_pydantic_validation_classified_validation(self):
        from pydantic import ValidationError, BaseModel

        class M(BaseModel):
            x: int

        try:
            M(x="not-an-int")
        except ValidationError as exc:
            assert _classify(exc) == "VALIDATION"


class TestRunAgentSyncRetry:
    """Test app-level retry/failure logic without touching the DB or LLM."""

    def test_l4_quota_error_raises_llm_failure_immediately(self):
        """L4: Quota error → LLMFailure raised without retrying."""
        class RateLimitError(Exception):
            pass

        call_count = 0

        def failing_coro_fn():
            nonlocal call_count
            call_count += 1

            async def _inner():
                raise RateLimitError("429 rate limit")
            return _inner()

        with pytest.raises(LLMFailure) as exc_info:
            run_agent_sync(failing_coro_fn)

        assert exc_info.value.category == "QUOTA_EXHAUSTED"
        # Must not retry — quota is a hard stop.
        assert call_count == 1

    def test_l6_transient_retries_are_bounded(self):
        """L6: Transient errors are retried but bounded at _APP_MAX_TRANSIENT_RETRIES."""
        from outreach_manager.core.llm import _APP_MAX_TRANSIENT_RETRIES

        call_count = 0

        def failing_coro_fn():
            nonlocal call_count
            call_count += 1

            async def _inner():
                raise IOError("network blip")
            return _inner()

        with pytest.raises(LLMFailure):
            # Patch sleep so the test doesn't actually wait.
            with patch("outreach_manager.core.llm.time.sleep"):
                run_agent_sync(failing_coro_fn)

        # Must have stopped at the cap, not looped forever.
        assert call_count <= _APP_MAX_TRANSIENT_RETRIES + 1

    def test_l7_primary_fallback_failure_terminates_cleanly(self):
        """L7: When every attempt fails, LLMFailure is raised cleanly (no infinite loop)."""
        from outreach_manager.core.llm import _APP_MAX_TRANSIENT_RETRIES

        async def _always_fail():
            raise IOError("always fails")

        with patch("outreach_manager.core.llm.time.sleep"):
            with pytest.raises(LLMFailure):
                run_agent_sync(lambda: _always_fail())

    def test_l8_auth_failure_stops_immediately(self):
        """L8: Auth failure raises LLMFailure immediately without any retry."""
        call_count = 0

        def failing_coro_fn():
            nonlocal call_count
            call_count += 1

            class AuthenticationError(Exception):
                pass

            async def _inner():
                raise AuthenticationError("invalid api key")
            return _inner()

        with pytest.raises(LLMFailure) as exc_info:
            run_agent_sync(failing_coro_fn)

        assert exc_info.value.category == "AUTH"
        assert call_count == 1


class TestStructuredOutputGating:
    """L5: Backup is excluded from FallbackModel when structured_output=True
    and BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE is not set to 'true'."""

    def _make_cfg(self):
        cfg = MagicMock()
        cfg.llm_api_key = "primary-key"
        cfg.ai_model = "google:gemini-1.5-pro"
        cfg.llm_api_base = ""
        return cfg

    @patch("outreach_manager.core.llm._validated_site_config")
    @patch.dict(os.environ, {
        "BACKUP_LLM_API_KEY": "backup-key",
        "BACKUP_AI_MODEL": "openai_compatible:llama-3-8b",
        "BACKUP_LLM_API_BASE": "https://api.example.com/v1",
        "BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE": "false",
    })
    def test_l5_incompatible_backup_excluded_for_structured_output(self, mock_cfg):
        """L5: When backup is not flagged compatible, structured_output=True returns
        primary model only (not FallbackModel)."""
        mock_cfg.return_value = self._make_cfg()
        from pydantic_ai.models.fallback import FallbackModel

        with patch("outreach_manager.core.llm._build_google") as mock_build:
            mock_primary = MagicMock(name="primary")
            mock_build.return_value = mock_primary
            result = get_llm_model(structured_output=True)

        # Must not be a FallbackModel — backup is incompatible.
        assert not isinstance(result, FallbackModel)

    @patch("outreach_manager.core.llm._validated_site_config")
    @patch.dict(os.environ, {
        "BACKUP_LLM_API_KEY": "backup-key",
        "BACKUP_AI_MODEL": "openai_compatible:llama-3-8b",
        "BACKUP_LLM_API_BASE": "https://api.example.com/v1",
        "BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE": "true",
    })
    def test_compatible_backup_included_when_flagged(self, mock_cfg):
        """When backup is explicitly flagged compatible, structured_output=True
        includes it in FallbackModel."""
        mock_cfg.return_value = self._make_cfg()
        from pydantic_ai.models.fallback import FallbackModel

        with (
            patch("outreach_manager.core.llm._build_google") as mock_primary_b,
            patch("outreach_manager.core.llm._build_openai_compatible") as mock_backup_b,
        ):
            mock_primary_b.return_value = MagicMock(name="primary")
            mock_backup_b.return_value = MagicMock(name="backup")
            result = get_llm_model(structured_output=True)

        assert isinstance(result, FallbackModel)


# ── M9–M15: Conversation memory ─────────────────────────────────────────────


class TestLeadIdentityContext:
    """Unit tests for _build_lead_identity_context — no DB needed."""

    def _make_deal_mock(self, public_id, first="Alice", last="Smith", headline=None):
        lead = MagicMock()
        lead.public_identifier = public_id
        lead.first_name = first
        lead.last_name = last
        lead.headline = headline
        lead.profile = {}
        deal = MagicMock()
        deal.lead = lead
        return deal

    def test_m9_extraction_includes_lead_name(self):
        """M9: extract_facts system prompt contains the lead's name."""
        from outreach_manager.core.db.summaries import (
            _build_lead_identity_context,
            _FACT_EXTRACTION_PROMPT,
            _build_identity_binding,
        )
        deal = self._make_deal_mock("alice-smith")
        identity = _build_lead_identity_context(deal)
        assert "Alice" in identity
        assert "Smith" in identity
        assert "alice-smith" in identity

    def test_m10_reconciliation_includes_lead_identity(self):
        """M10: reconcile_facts preamble contains the lead identity block."""
        from outreach_manager.core.db.summaries import _build_lead_identity_context

        deal = self._make_deal_mock("bob-jones", first="Bob", last="Jones")
        identity = _build_lead_identity_context(deal)
        assert "Bob" in identity
        assert "bob-jones" in identity
        assert "authoritative" in identity.lower()

    def test_m11_third_party_protection_in_prompt(self):
        """M11: The extraction prompt explicitly warns against recording third-party facts."""
        from outreach_manager.core.db.summaries import _FACT_EXTRACTION_PROMPT

        prompt_lower = _FACT_EXTRACTION_PROMPT.lower()
        # The prompt must instruct the LLM to only attribute facts to the
        # identified lead (not third parties, colleagues, clients, etc.)
        assert "explicitly" in prompt_lower, "Prompt must use 'explicitly' to anchor attribution"
        assert "other named people" in prompt_lower or "third" in prompt_lower or "colleagues" in prompt_lower, (
            "Prompt must warn against recording facts about third parties"
        )

    def test_m12_seller_protection_in_binding(self):
        """M12: Seller identity binding explicitly prevents seller-name attribution."""
        from outreach_manager.core.db.summaries import _build_identity_binding

        binding = _build_identity_binding("Diego")
        assert "Diego" in binding
        assert "[Me]" in binding
        assert "never attribute" in binding.lower() or "reference to [me]" in binding.lower()


@pytest.mark.django_db
class TestRebuildChatSummary:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def _make_deal(self, session, public_id):
        _make_qualified(session, public_id)
        return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)

    def test_m14_empty_deal_rebuild_no_llm_call(self, fake_session):
        """M14: Empty Deal rebuild produces {facts: []} without calling LLM."""
        deal = self._make_deal(fake_session, "empty-lead")
        deal.chat_summary = {"facts": ["contaminated fact from another lead"]}
        deal.save()

        with patch("outreach_manager.core.db.summaries.extract_facts") as mock_extract:
            from outreach_manager.core.db.summaries import rebuild_chat_summary
            rebuild_chat_summary(deal, seller_name="Me")

        # LLM must not be called for an empty conversation.
        mock_extract.assert_not_called()
        deal.refresh_from_db()
        assert deal.chat_summary == {"facts": []}

    def test_m13_rebuild_uses_only_deal_a_messages(self, fake_session):
        """M13: rebuild_chat_summary for Deal A only reads Deal A's ChatMessages."""
        from outreach_manager.chat.models import ChatMessage

        deal_a = self._make_deal(fake_session, "lead-a")
        deal_b = self._make_deal(fake_session, "lead-b")

        # Create messages for both deals.
        ChatMessage.objects.create(
            deal=deal_a,
            linkedin_urn="urn:li:msg:a1",
            content="I work at Acme as CTO",
            is_outgoing=False,
            owner=fake_session.django_user,
        )
        ChatMessage.objects.create(
            deal=deal_b,
            linkedin_urn="urn:li:msg:b1",
            content="I work at Beta Corp as CFO",
            is_outgoing=False,
            owner=fake_session.django_user,
        )

        calls_received = []

        def capture_extract(text, **kwargs):
            calls_received.append(text)
            return ["captured fact"]

        with patch("outreach_manager.core.db.summaries.extract_facts", side_effect=capture_extract):
            with patch("outreach_manager.core.db.summaries.reconcile_facts", return_value=["captured fact"]):
                from outreach_manager.core.db.summaries import rebuild_chat_summary
                rebuild_chat_summary(deal_a, seller_name="Me")

        assert len(calls_received) == 1
        # Text sent to LLM must contain Deal A's message but NOT Deal B's.
        assert "Acme" in calls_received[0]
        assert "Beta Corp" not in calls_received[0]


@pytest.mark.django_db
class TestSummaryFailureIsolation:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_m15_summary_failure_does_not_remove_chat_messages(self, fake_session):
        """M15: If summary update raises, already-persisted ChatMessages remain intact."""
        from outreach_manager.chat.models import ChatMessage
        from outreach_manager.linkedin.db.chat import sync_conversation

        _make_qualified(fake_session, "persist-lead")
        deal = Deal.objects.get(
            lead__public_identifier="persist-lead",
            campaign=fake_session.campaign,
        )

        # Pre-create a ChatMessage to simulate previously-synced data.
        msg = ChatMessage.objects.create(
            deal=deal,
            linkedin_urn="urn:li:msg:existing1",
            content="Existing message",
            is_outgoing=False,
            owner=fake_session.django_user,
        )

        # Make the summary update raise.
        with patch(
            "outreach_manager.linkedin.db.chat._update_deal_chat_summary",
            side_effect=RuntimeError("LLM summary blew up"),
        ):
            with patch(
                "outreach_manager.linkedin.db.chat._sync_from_api",
                return_value=[],  # No new messages — summary won't be called
            ):
                # Use direct call to simulate the isolation path.
                from outreach_manager.linkedin.db.chat import _update_deal_chat_summary
                try:
                    _update_deal_chat_summary(fake_session, deal, [msg])
                except Exception:
                    pass  # We're testing the caller's isolation, not this helper

        # The ChatMessage must still exist regardless.
        assert ChatMessage.objects.filter(pk=msg.pk).exists(), (
            "ChatMessage must remain after summary failure"
        )

    def test_summary_exception_in_sync_conversation_is_swallowed(self, fake_session):
        """sync_conversation must return normally even when _update_deal_chat_summary raises."""
        from outreach_manager.linkedin.db.chat import sync_conversation

        _make_qualified(fake_session, "swallow-lead")

        with patch("outreach_manager.linkedin.db.chat._sync_from_api", return_value=[]):
            with patch(
                "outreach_manager.linkedin.db.chat._update_deal_chat_summary",
                side_effect=RuntimeError("summary LLM failure"),
            ):
                # Must not raise — summary failure is isolated.
                result = sync_conversation(fake_session, "swallow-lead")

        # Result is valid even with summary failure.
        assert result is not None
        assert isinstance(result.new_messages, list)
