# tests/test_reconcile.py
"""Tests for scheduler.py reconcile() under the Decoupled Probabilistic Engine.

Under the new engine:
  - reconcile() resets stale RUNNING tasks to PENDING.
  - reconcile() plans CHECK_PENDING slots for due PENDING deals.
  - reconcile() flushes email queue.
  - Cycle-driven tasks (CONNECT, FOLLOW_UP, REPLY_UNREAD, FIRST_MESSAGE, EXTRACT_LEADS)
    do NOT use DB task rows (driven by BalancedSequenceGenerator + SyntheticTask).
"""
import pytest
from unittest.mock import patch
from django.utils import timezone

from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.models import Task
from linkedin_cli.enums import ProfileState
from outreach_manager.linkedin.scheduler import reconcile


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _make_pending(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)


@pytest.mark.django_db
@patch("outreach_manager.core.scheduler.ENABLE_ACTIVE_HOURS", False)
class TestReconcile:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_recovers_stale_running_tasks(self, fake_session):
        Task.objects.create(
            task_type=Task.TaskType.CHECK_PENDING,
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        reconcile(fake_session)
        assert Task.objects.filter(status=Task.Status.RUNNING).count() == 0
        assert Task.objects.filter(
            task_type=Task.TaskType.CHECK_PENDING,
            status=Task.Status.PENDING,
        ).exists()

    def test_plans_check_pending_slots_for_due_deals(self, fake_session):
        _make_pending(fake_session, "alice")
        from outreach_manager.crm.models import Deal
        Deal.objects.filter(lead__public_identifier="alice").update(
            next_check_pending_at=timezone.now(),
        )
        Task.objects.all().delete()

        reconcile(fake_session)
        assert Task.objects.filter(
            task_type=Task.TaskType.CHECK_PENDING,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).count() == 1

    def test_does_not_replan_check_pending_when_pending_exists(self, fake_session):
        _make_pending(fake_session, "alice")
        from outreach_manager.crm.models import Deal
        Deal.objects.filter(lead__public_identifier="alice").update(
            next_check_pending_at=timezone.now(),
        )
        reconcile(fake_session)
        count_before = Task.objects.filter(status=Task.Status.PENDING).count()
        reconcile(fake_session)
        count_after = Task.objects.filter(status=Task.Status.PENDING).count()
        assert count_before == count_after == 1

    def test_cycle_driven_tasks_not_queued_by_reconcile(self, fake_session):
        """Cycle-driven workflows use SyntheticTask and are not pre-queued as DB rows by reconcile."""
        reconcile(fake_session)
        cycle_types = [
            Task.TaskType.CONNECT,
            Task.TaskType.FOLLOW_UP,
            Task.TaskType.REPLY_UNREAD,
            Task.TaskType.FIRST_MESSAGE,
            Task.TaskType.EXTRACT_LEADS,
        ]
        assert not Task.objects.filter(
            task_type__in=cycle_types,
            status=Task.Status.PENDING,
        ).exists()
