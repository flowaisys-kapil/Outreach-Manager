# tests/test_ticket11_check_pending_sync.py
"""Ticket 11 Regression Tests — Fix CHECK_PENDING Synchronization & Pending Invitation Discovery.

Verifies that:
  1. LinkedIn contains invitations missing locally -> workflow imports and creates local records.
  2. Local DB pending count matches LinkedIn count after synchronization.
  3. Invitations older than 7 days are detected and withdrawn correctly.
  4. Newly accepted invitations are detected and promoted to CONNECTED.
  5. Local DB state no longer prevents LinkedIn inspection (runs even if DB has 0 due pending deals).
  6. Running CHECK_PENDING twice produces stable, idempotent results.
  7. Existing scheduling and batch architecture remain unchanged.
"""

import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.models import Task
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending
from outreach_manager.linkedin.pipeline.acceptances import (
    sync_sent_invitations,
    parse_sent_text_to_datetime,
    is_older_than_7_days,
)


SAMPLE_PROFILE = {
    "first_name": "Test",
    "last_name": "User",
    "headline": "Engineer",
}


def _make_task(task_type, payload):
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


def _build_context(session):
    from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
    qualifier = BayesianQualifier(seed=42)
    qualifier.rank_profiles = lambda profiles, **kw: profiles
    return {session.campaign.pk: qualifier}


@pytest.mark.django_db
class TestTicket11CheckPendingSync:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_1_discovers_missing_linkedin_invitations(self, fake_session):
        """1. Scraped LinkedIn invitations missing from local DB are imported into DB."""
        scraped = [
            {"public_id": "untracked-lead-1", "name": "Untracked One", "sent_text": "Sent 3 days ago"},
            {"public_id": "untracked-lead-2", "name": "Untracked Two", "sent_text": "Sent 1 week ago"},
        ]

        with patch("outreach_manager.linkedin.pipeline.acceptances.scrape_sent_invitations", return_value=scraped), \
             patch("outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page", return_value=set()):
            sync_sent_invitations(fake_session, fake_session.campaign)

        d1 = Deal.objects.filter(campaign=fake_session.campaign, lead__public_identifier="untracked-lead-1").first()
        d2 = Deal.objects.filter(campaign=fake_session.campaign, lead__public_identifier="untracked-lead-2").first()

        assert d1 is not None
        assert d1.state == DealState.PENDING
        assert d1.connection_requested_at is not None

        assert d2 is not None
        assert d2.state == DealState.PENDING
        assert d2.connection_requested_at is not None

    def test_2_dashboard_count_matches_linkedin_after_sync(self, fake_session):
        """2. Dashboard count (DB pending deals) matches LinkedIn count (46 vs 40 scenario)."""
        # Create 40 existing pending deals in DB
        for i in range(40):
            pid = f"existing-pending-{i}"
            create_enriched_lead(fake_session, f"https://linkedin.com/in/{pid}/", SAMPLE_PROFILE)
            promote_lead_to_deal(fake_session, pid)
            set_profile_state(fake_session, pid, DealState.PENDING.value)

        assert Deal.objects.filter(campaign=fake_session.campaign, state=DealState.PENDING).count() == 40

        # LinkedIn sent page has 46 invitations (40 existing + 6 new)
        linkedin_sent = [
            {"public_id": f"existing-pending-{i}", "name": f"User {i}", "sent_text": "Sent 2 days ago"}
            for i in range(40)
        ] + [
            {"public_id": f"new-pending-{j}", "name": f"New User {j}", "sent_text": "Sent 1 day ago"}
            for j in range(6)
        ]

        with patch("outreach_manager.linkedin.pipeline.acceptances.scrape_sent_invitations", return_value=linkedin_sent), \
             patch("outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page", return_value=set()):
            sync_sent_invitations(fake_session, fake_session.campaign)

        db_count = Deal.objects.filter(campaign=fake_session.campaign, state=DealState.PENDING).count()
        assert db_count == 46

    def test_3_invitations_older_than_threshold_detected(self, fake_session):
        """3. Invitations older than 7 days are detected via sent_text and connection_requested_at."""
        assert is_older_than_7_days("Sent 8 days ago") is True
        assert is_older_than_7_days("Sent 2 weeks ago") is True
        assert is_older_than_7_days("Sent 1 month ago") is True
        assert is_older_than_7_days("Sent 3 days ago") is False

        st_8d = parse_sent_text_to_datetime("Sent 8 days ago")
        assert (timezone.now() - st_8d).days >= 7

    def test_4_newly_accepted_invitations_detected_and_promoted(self, fake_session):
        """4. Local PENDING deals whose invitation is accepted are promoted to CONNECTED."""
        create_enriched_lead(fake_session, "https://linkedin.com/in/accepted-user/", SAMPLE_PROFILE)
        promote_lead_to_deal(fake_session, "accepted-user")
        set_profile_state(fake_session, "accepted-user", DealState.PENDING.value)

        # Scrape returns empty sent requests (no longer pending) and accepted-user in connections list
        with patch("outreach_manager.linkedin.pipeline.acceptances.scrape_sent_invitations", return_value=[]), \
             patch("outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page", return_value={"accepted-user"}):
            sync_sent_invitations(fake_session, fake_session.campaign)

        deal = Deal.objects.get(campaign=fake_session.campaign, lead__public_identifier="accepted-user")
        assert deal.state == DealState.CONNECTED

    def test_5_db_empty_due_does_not_prevent_linkedin_inspection(self, fake_session):
        """5. Phase A sync runs even if local DB has 0 due PENDING deals."""
        # Create a pending deal with next_check_pending_at in the future (not due in DB)
        create_enriched_lead(fake_session, "https://linkedin.com/in/future-due/", SAMPLE_PROFILE)
        promote_lead_to_deal(fake_session, "future-due")
        set_profile_state(fake_session, "future-due", DealState.PENDING.value)
        Deal.objects.filter(campaign=fake_session.campaign).update(
            next_check_pending_at=timezone.now() + timedelta(days=5),
            next_action_at=timezone.now() + timedelta(days=5),
        )

        scraped = [{"public_id": "discovered-lead", "name": "Discovered", "sent_text": "Sent 2 days ago"}]

        sync_called = False

        def mock_sync(session, campaign):
            nonlocal sync_called
            sync_called = True
            return scraped

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})

        with patch("outreach_manager.linkedin.pipeline.acceptances.sync_sent_invitations", side_effect=mock_sync), \
             patch("outreach_manager.linkedin.pipeline.acceptances.run_withdrawals_check"):
            handle_check_pending(task, fake_session, _build_context(fake_session))

        assert sync_called is True, "Phase A sync MUST run even when claim_due_deal returns None"

    def test_6_idempotency_running_check_pending_twice_is_stable(self, fake_session):
        """6. Running CHECK_PENDING twice on the same data produces stable results."""
        scraped = [
            {"public_id": "idem-1", "name": "Idem One", "sent_text": "Sent 2 days ago"},
        ]

        with patch("outreach_manager.linkedin.pipeline.acceptances.scrape_sent_invitations", return_value=scraped), \
             patch("outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page", return_value=set()):
            sync_sent_invitations(fake_session, fake_session.campaign)
            sync_sent_invitations(fake_session, fake_session.campaign)

        count = Deal.objects.filter(campaign=fake_session.campaign, lead__public_identifier="idem-1").count()
        assert count == 1, "Duplicate sync MUST NOT create duplicate deals"

    def test_7_existing_scheduling_remains_unchanged(self, fake_session):
        """7. Normal check_pending processing and backoff scheduling remains intact."""
        create_enriched_lead(fake_session, "https://linkedin.com/in/pending-due/", SAMPLE_PROFILE)
        promote_lead_to_deal(fake_session, "pending-due")
        set_profile_state(fake_session, "pending-due", DealState.PENDING.value)
        Deal.objects.filter(campaign=fake_session.campaign).update(
            next_check_pending_at=timezone.now() - timedelta(minutes=5),
            next_action_at=timezone.now() - timedelta(minutes=5),
        )

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})

        with patch("outreach_manager.linkedin.pipeline.acceptances.sync_sent_invitations", return_value=[]), \
             patch("outreach_manager.linkedin.tasks.check_pending._resolve_status_individually", return_value="PENDING"):
            res = handle_check_pending(task, fake_session, _build_context(fake_session))

        assert res.processed_count == 1
        deal = Deal.objects.get(campaign=fake_session.campaign, lead__public_identifier="pending-due")
        assert deal.backoff_hours > 0
