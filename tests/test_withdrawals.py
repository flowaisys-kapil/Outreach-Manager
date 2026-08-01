# tests/test_withdrawals.py
import pytest
from unittest.mock import patch
from django.utils import timezone
from datetime import timedelta

from outreach_manager.crm.models import Deal, DealState, Lead, Outcome
from outreach_manager.core.db.deals import get_qualified_profiles, set_profile_state
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.linkedin.pipeline.acceptances import (
    is_older_than_7_days,
    withdraw_deal,
    run_withdrawals_check,
)

SAMPLE_PROFILE = {
    "first_name": "John",
    "last_name": "Doe",
    "headline": "Software Architect",
    "positions": [{"company_name": "Globex"}],
}


def _make_deal(session, public_id, state=DealState.PENDING):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, state.value)
    return Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)


@pytest.mark.django_db
class TestWithdrawalsAndRetries:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_is_older_than_7_days(self):
        assert is_older_than_7_days("Sent 8 days ago") is True
        assert is_older_than_7_days("Sent 1 week ago") is True
        assert is_older_than_7_days("Sent 2 weeks ago") is True
        assert is_older_than_7_days("Sent 1 month ago") is True
        assert is_older_than_7_days("Sent 6 days ago") is False
        assert is_older_than_7_days("Sent 8 minutes ago") is False
        assert is_older_than_7_days("Sent 2 hours ago") is False

    def test_withdraw_deal_first_attempt(self, fake_session):
        campaign = fake_session.campaign
        deal = _make_deal(fake_session, "lead1", DealState.PENDING)
        assert deal.withdraw_count == 0
        assert deal.last_withdrawn_at is None

        withdraw_deal(fake_session, campaign, "lead1")

        deal.refresh_from_db()
        assert deal.withdraw_count == 1
        assert deal.last_withdrawn_at is not None
        assert deal.state == DealState.QUALIFIED

    def test_withdraw_deal_max_attempts(self, fake_session):
        campaign = fake_session.campaign
        deal = _make_deal(fake_session, "lead2", DealState.PENDING)
        deal.withdraw_count = 2
        deal.save()

        withdraw_deal(fake_session, campaign, "lead2")

        deal.refresh_from_db()
        assert deal.withdraw_count == 3
        assert deal.state == DealState.FAILED
        assert deal.outcome == Outcome.UNRESPONSIVE

    def test_cooldown_filter_get_qualified_profiles(self, fake_session):
        # Create three qualified deals
        deal_no_withraw = _make_deal(fake_session, "lead_ok", DealState.QUALIFIED)
        
        deal_old_withraw = _make_deal(fake_session, "lead_old", DealState.QUALIFIED)
        deal_old_withraw.last_withdrawn_at = timezone.now() - timedelta(days=22)
        deal_old_withraw.save()

        deal_recent_withdraw = _make_deal(fake_session, "lead_cooldown", DealState.QUALIFIED)
        deal_recent_withdraw.last_withdrawn_at = timezone.now() - timedelta(days=5)
        deal_recent_withdraw.save()

        profiles = get_qualified_profiles(fake_session)
        pids = [p["public_identifier"] for p in profiles]

        assert "lead_ok" in pids
        assert "lead_old" in pids
        assert "lead_cooldown" not in pids  # should be filtered out by 21-day cooldown!

    def test_run_withdrawals_check_test_mode(self, fake_session):
        campaign = fake_session.campaign
        
        # Pending — connection requested 10 days ago (should be withdrawn)
        deal_old = _make_deal(fake_session, "lead_old_pending", DealState.PENDING)
        deal_old.connection_requested_at = timezone.now() - timedelta(days=10)
        deal_old.save()

        # Pending — connection requested 2 days ago (should NOT be withdrawn)
        deal_recent = _make_deal(fake_session, "lead_new_pending", DealState.PENDING)
        deal_recent.connection_requested_at = timezone.now() - timedelta(days=2)
        deal_recent.save()

        # Run withdrawals check in mock mode (no page attribute on session)
        run_withdrawals_check(fake_session, campaign)

        deal_old.refresh_from_db()
        deal_recent.refresh_from_db()

        assert deal_old.state == DealState.QUALIFIED
        assert deal_old.withdraw_count == 1
        
        assert deal_recent.state == DealState.PENDING
        assert deal_recent.withdraw_count == 0

    def test_old_deal_recent_request_not_eligible(self, fake_session):
        """A deal with an old creation_date but recent connection_requested_at
        must NOT be withdrawn — eligibility uses only connection_requested_at."""
        campaign = fake_session.campaign

        deal = _make_deal(fake_session, "lead_trick", DealState.PENDING)
        # Old Deal, but the invitation was only sent 3 days ago.
        deal.creation_date = timezone.now() - timedelta(days=15)
        deal.connection_requested_at = timezone.now() - timedelta(days=3)
        deal.save()

        run_withdrawals_check(fake_session, campaign)

        deal.refresh_from_db()
        assert deal.state == DealState.PENDING, (
            "Deal with recent connection_requested_at must not be withdrawn "
            "even if creation_date is old"
        )
        assert deal.withdraw_count == 0
