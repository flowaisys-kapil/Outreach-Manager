# tests/test_extraction_and_prechecks.py
import pytest
from unittest.mock import patch, MagicMock
from django.utils import timezone
from datetime import timedelta

from outreach_manager.crm.models import Deal, DealState, Lead
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.linkedin.pipeline.acceptances import run_acceptance_sweep
from outreach_manager.linkedin.tasks.connect import handle_connect
from outreach_manager.core.models import SiteConfig, Task

SAMPLE_PROFILE = {
    "first_name": "Jane",
    "last_name": "Doe",
    "headline": "VP Product",
    "positions": [{"company_name": "Initech"}],
}


def _make_deal(session, public_id, state=DealState.CONNECTED):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)
    set_profile_state(session, public_id, state.value)
    deal = Deal.objects.get(lead__public_identifier=public_id, campaign=session.campaign)
    deal.profile_summary = {"headline": "VP Product", "summary": "Product leader"}
    deal.save()
    return deal


@pytest.mark.django_db
class TestAcceptanceAndPreChecks:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_run_acceptance_sweep_promotes_accepted_deals(self, fake_session):
        campaign = fake_session.campaign
        deal = _make_deal(fake_session, "pending_lead", DealState.PENDING)

        with patch("outreach_manager.linkedin.pipeline.acceptances.check_acceptances_page", return_value={"pending_lead"}):
            promoted = run_acceptance_sweep(fake_session, campaign)
            deal.refresh_from_db()
            assert promoted == 1
            assert deal.state == DealState.CONNECTED

    def test_run_jit_pre_checks_randomization(self, fake_session):
        from outreach_manager.core.sequence_generator import BalancedSequenceGenerator
        seq = BalancedSequenceGenerator.get_cycle_sequence(fake_session)
        # All 6 workflows must be included in every cycle sequence
        assert len(seq) == 6
        assert set(seq) == {
            Task.TaskType.REPLY_UNREAD, Task.TaskType.FOLLOW_UP, Task.TaskType.FIRST_MESSAGE,
            Task.TaskType.CHECK_PENDING, Task.TaskType.CONNECT, Task.TaskType.EXTRACT_LEADS
        }

    def test_workflow_lock_exclusive_acquisition(self):
        """WorkflowLock must not allow concurrent entry."""
        from outreach_manager.core.sequence_generator import WorkflowLock
        import threading

        results = []
        barrier = threading.Barrier(2)

        def _try_acquire(name):
            try:
                # Lock with a tiny timeout so the second thread fails fast
                acquired = WorkflowLock._lock.acquire(timeout=0.1)
                results.append(("acquired", name, acquired))
                if acquired:
                    WorkflowLock._lock.release()
            except Exception as e:
                results.append(("error", name, str(e)))

        with WorkflowLock.acquire("workflow_a"):
            # Attempt a second acquisition while first is held — must fail (timeout)
            t = threading.Thread(target=_try_acquire, args=("workflow_b",))
            t.start()
            t.join()

        # The second thread should have failed to acquire
        assert any(r[0] == "acquired" and r[2] is False for r in results), (
            "WorkflowLock should block concurrent acquisition"
        )

    def test_extract_mode_forces_search_extraction(self, fake_session):
        campaign = fake_session.campaign
        task = Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            payload={"campaign_id": campaign.pk},
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
        )

        site_config = SiteConfig.load()
        site_config.simulated_task = "extract"
        site_config.override_expires_at = timezone.now() + timedelta(minutes=30)
        site_config.save()

        def fake_qualify(session, qualifier):
            yield "extracted_lead_99"

        with patch("outreach_manager.linkedin.pipeline.pools.qualify_source", side_effect=fake_qualify):
            result = handle_connect(task, fake_session, qualifiers={})
            assert bool(result) is True
