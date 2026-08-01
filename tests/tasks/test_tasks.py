# tests/tasks/test_tasks.py
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from outreach_manager.crm.models import Deal
from outreach_manager.core.agents.follow_up import FollowUpDecision
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.models import Task
from outreach_manager.linkedin.models import ActionLog
from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
from linkedin_cli.enums import ProfileState
from linkedin_cli.exceptions import SkipProfile, ReachedConnectionLimit
from outreach_manager.linkedin.tasks.connect import ConnectStrategy, handle_connect
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _mock_strategy(candidate, qualifier=None):
    """Build a ConnectStrategy that returns a fixed candidate once, then None.

    Without the None terminator the while-can_execute loop in handle_connect
    keeps returning the same candidate after it is already PENDING/CONNECTED,
    hitting the 'already processed' guard and looping forever.
    """
    candidates = iter([candidate])  # Returns candidate once, then StopIteration

    def _find(s):
        try:
            return next(candidates)
        except StopIteration:
            return None

    return ConnectStrategy(
        find_candidate=_find,
        pre_connect=None,
        qualifier=qualifier or MagicMock(explain=lambda *a, **kw: ""),
    )


def _assert_deal_state(session, public_id, expected_state: ProfileState):
    from outreach_manager.crm.models import Deal
    deal = Deal.objects.get(
        lead__linkedin_url=f"https://www.linkedin.com/in/{public_id}/",
        campaign=session.campaign,
    )
    assert deal.state == expected_state


def _make_qualified(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)


def _make_pending_due(session, public_id="alice"):
    """Create a PENDING deal whose ``next_check_pending_at`` is overdue."""
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)
    from outreach_manager.crm.models import Deal
    Deal.objects.filter(lead__public_identifier=public_id).update(
        next_check_pending_at=timezone.now() - timedelta(minutes=1),
    )
    Task.objects.all().delete()


def _make_connected(session, public_id="alice"):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)
    Task.objects.all().delete()


def _make_task(task_type, payload, **kwargs):
    """Create a lazy task and mark it RUNNING (matching daemon behavior)."""
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.RUNNING,
        scheduled_at=kwargs.pop("scheduled_at", timezone.now()),
        started_at=timezone.now(),
        payload=payload,
        **kwargs,
    )


def _build_context(fake_session):
    """Build qualifiers dict for task handlers."""
    qualifier = BayesianQualifier(seed=42)
    qualifier.rank_profiles = lambda profiles, **kw: profiles
    return {fake_session.campaign.pk: qualifier}


# ── handle_connect tests ────────────────────────────────────────


@pytest.mark.django_db
class TestHandleConnect:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def _candidate(self):
        return {"public_identifier": "alice", "url": "https://www.linkedin.com/in/alice/", "profile": SAMPLE_PROFILE}

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch("linkedin_cli.actions.connect.send_connection_request")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_sends_connection_and_records(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.PENDING)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.CONNECT).count() == 1

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch("linkedin_cli.actions.connect.send_connection_request")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_pending_stamps_next_check(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        """Connect → PENDING: state hook should stamp next_check_pending_at, no follow-up Task."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.next_check_pending_at is not None
        assert not Task.objects.filter(task_type=Task.TaskType.CHECK_PENDING).exists()

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_marks_preexisting_connected(self, mock_status, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.CONNECTED

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        # Lazy model: no follow_up Task is created on CONNECTED state entry.
        assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_handles_rate_limit(self, mock_status, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.side_effect = ReachedConnectionLimit("weekly limit")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        assert ActionLog.ActionType.CONNECT in fake_session.linkedin_profile._exhausted

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch("linkedin_cli.actions.connect.send_connection_request")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_handles_skip_profile(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.side_effect = SkipProfile("bad profile")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.FAILED)

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    def test_skips_when_no_candidate(self, mock_strategy, fake_session):
        """Lazy: handler marks the slot done; planner re-plans next window."""
        mock_strategy.return_value = _mock_strategy(None)

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        # No new tasks created by the handler.
        assert Task.objects.exclude(pk=task.pk).count() == 0

    def test_skips_when_rate_limited(self, fake_session):
        fake_session.linkedin_profile.connect_daily_limit = 0
        fake_session.linkedin_profile.save(update_fields=["connect_daily_limit"])

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        assert Task.objects.exclude(pk=task.pk).count() == 0


# ── handle_check_pending tests ──────────────────────────────────────


@pytest.mark.django_db
class TestHandleCheckPending:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_transitions_due_deal_to_connected(self, mock_status, fake_session):
        mock_status.return_value = ProfileState.CONNECTED
        _make_pending_due(fake_session)

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        # Lazy model: no follow_up task is auto-enqueued on the transition.
        assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_stays_pending_doubles_backoff_and_restamps(self, mock_status, fake_session):
        mock_status.return_value = ProfileState.PENDING
        _make_pending_due(fake_session)
        from linkedin_cli.url_utils import public_id_to_url
        Deal.objects.filter(lead__linkedin_url=public_id_to_url("alice")).update(backoff_hours=72)

        before = timezone.now()
        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__linkedin_url=public_id_to_url("alice"))
        assert deal.backoff_hours == 144  # 72 × 2
        # next_check_pending_at re-stamped by the state hook to now + 144h
        expected = before + timedelta(hours=144)
        assert abs((deal.next_check_pending_at - expected).total_seconds()) < 10

    def test_skips_when_no_due_deals(self, fake_session):
        # No PENDING deals at all → handler marks slot done.
        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))
        # No exceptions; no new tasks created.
        assert Task.objects.exclude(pk=task.pk).count() == 0

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_ignores_not_yet_due_pending(self, mock_status, fake_session):
        """A PENDING deal whose next_check is in the future should not be picked."""
        _make_qualified(fake_session, "alice")
        set_profile_state(fake_session, "alice", ProfileState.PENDING.value)
        Deal.objects.filter(lead__public_identifier="alice").update(
            next_check_pending_at=timezone.now() + timedelta(hours=10),
        )
        Task.objects.all().delete()

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        res = handle_check_pending(task, fake_session, _build_context(fake_session))
        deal = Deal.objects.get(lead__public_identifier="alice")
        assert res.processed_count == 0
        assert deal.backoff_hours == 0


# ── handle_follow_up tests ─────────────────────────────────────


@pytest.mark.django_db
class TestHandleFollowUp:
    @patch("outreach_manager.linkedin.db.chat.sync_conversation")
    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin_cli.actions.message.send_raw_message", return_value=True)
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    @patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False)
    @patch("outreach_manager.linkedin.scheduler.claim_due_deal")
    def test_send_message_records_action(self, mock_claim, mock_nudge, mock_capture, mock_agent, mock_send, mock_materialize, mock_sync, fake_session):
        mock_agent.return_value = FollowUpDecision(
            action="send_message", message="Hello Alice, great to connect with you!", follow_up_hours=72,
        )
        _make_connected(fake_session)
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        mock_claim.side_effect = [deal, None]  # Return deal once, then stop

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        # Lazy: agent gets the resolved Deal.
        mock_materialize.assert_called_once()
        materialized_deal = mock_materialize.call_args[0][0]
        assert materialized_deal.lead.public_identifier == "alice"

        mock_agent.assert_called_once()
        agent_deal = mock_agent.call_args[0][1]
        assert agent_deal.lead.public_identifier == "alice"

        mock_send.assert_called_once()
        mock_sync.assert_called_once_with(fake_session, "alice")
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 1

    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin_cli.actions.message.send_raw_message", return_value=False)
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    @patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False)
    @patch("outreach_manager.linkedin.scheduler.claim_due_deal")
    def test_send_failure_preserves_connected(self, mock_claim, mock_nudge, mock_capture, mock_agent, mock_send, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(
            action="send_message", message="Hi Alice, checking in on our conversation!", follow_up_hours=24,
        )
        _make_connected(fake_session)
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        mock_claim.side_effect = [deal, None]

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.state == ProfileState.CONNECTED

    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    @patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False)
    @patch("outreach_manager.linkedin.scheduler.claim_due_deal")
    def test_mark_completed_sets_state(self, mock_claim, mock_nudge, mock_capture, mock_agent, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(
            action="mark_completed", outcome="unresponsive", follow_up_hours=0,
        )
        _make_connected(fake_session)
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        mock_claim.side_effect = [deal, None]

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.state == ProfileState.COMPLETED
        assert deal.outcome == "unresponsive"

    @patch("outreach_manager.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    @patch("outreach_manager.core.db.deals.capture_and_contribute")
    @patch("outreach_manager.linkedin.tasks.follow_up._too_soon_to_nudge", return_value=False)
    @patch("outreach_manager.linkedin.scheduler.claim_due_deal")
    def test_wait_bumps_update_date(self, mock_claim, mock_nudge, mock_capture, mock_agent, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(action="wait", follow_up_hours=48)
        _make_connected(fake_session)
        deal_before = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        from datetime import timedelta
        deal_before.update_date = deal_before.update_date - timedelta(seconds=5)
        deal_before.save(update_fields=["update_date"])
        original_update = deal_before.update_date
        mock_claim.side_effect = [deal_before, None]

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        deal_after = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal_after.update_date > original_update

    @patch("outreach_manager.core.agents.follow_up.run_follow_up_agent")
    def test_skips_when_no_eligible_deal(self, mock_agent, fake_session):
        """No CONNECTED deals → handler marks slot done without calling the agent."""
        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))
        mock_agent.assert_not_called()

    def test_skips_on_rate_limit(self, fake_session):
        _make_connected(fake_session)
        fake_session.linkedin_profile.follow_up_daily_limit = 0
        fake_session.linkedin_profile.save(update_fields=["follow_up_daily_limit"])

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        # No follow-up was actually performed (no ActionLog row).
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
