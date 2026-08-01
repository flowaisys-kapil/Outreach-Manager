# tests/test_phase3_connection_lifecycle.py
"""Phase 3 regression tests — Connection Lifecycle Reliability.

Covers:
  P3-1: connection_requested_at is stamped on PENDING transition.
  P3-2: Withdrawal age uses connection_requested_at (not creation_date).
  P3-3: Legacy deals (NULL connection_requested_at) fall back to creation_date.
  P3-4: UNKNOWN browser observation does not corrupt Deal state.
  P3-5: CONNECTED→PENDING illegal transition is rejected with ValueError.
  P3-6: CONNECTED→QUALIFIED illegal transition is rejected with ValueError.
  P3-7: COMPLETED→QUALIFIED illegal transition is rejected with ValueError.
  P3-8: Permitted transitions (any → FAILED, PENDING → CONNECTED) are allowed.
  P3-9: Broad-scrape CONNECTED path promotes deal (regression guard).
"""
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from outreach_manager.crm.models import Deal, DealState
from outreach_manager.core.db.deals import set_profile_state
from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.models import Task
from linkedin_cli.enums import ProfileState
from linkedin_cli.exceptions import ReachedConnectionLimit, SkipProfile
from outreach_manager.linkedin.tasks.connect import ConnectStrategy, handle_connect
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending, _resolve_status_individually
from outreach_manager.linkedin.pipeline.acceptances import _connection_request_age_filter


SAMPLE_PROFILE = {
    "first_name": "Bob",
    "last_name": "Jones",
    "headline": "Developer",
    "positions": [{"company_name": "Initech"}],
}


def _make_qualified(session, public_id="bob"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)


def _make_pending_due(session, public_id="bob"):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)
    Deal.objects.filter(lead__public_identifier=public_id).update(
        next_check_pending_at=timezone.now() - timedelta(minutes=1),
    )
    Task.objects.all().delete()


def _make_task(task_type, payload):
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


def _mock_strategy(candidate):
    """Return a ConnectStrategy whose find_candidate yields the candidate once then None.

    Without the None terminator the while-can_execute loop in handle_connect
    loops forever once the deal is PENDING/CONNECTED (hits the skip guard).
    """
    candidates = iter([candidate])

    def _find(s):
        try:
            return next(candidates)
        except StopIteration:
            return None

    return ConnectStrategy(
        find_candidate=_find,
        pre_connect=None,
        qualifier=MagicMock(explain=lambda *a, **kw: ""),
    )


def _build_context(session):
    from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
    qualifier = BayesianQualifier(seed=42)
    qualifier.rank_profiles = lambda profiles, **kw: profiles
    return {session.campaign.pk: qualifier}


# ── P3-1: connection_requested_at is stamped on PENDING transition ──────────


@pytest.mark.django_db
class TestConnectionRequestedAtStamping:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.connect.send_connection_request")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_pending_stamps_connection_requested_at(
        self, mock_status, mock_send, mock_strategy, fake_session
    ):
        """A successful PENDING transition must stamp connection_requested_at."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(
            {"public_identifier": "bob", "url": "https://www.linkedin.com/in/bob/", "profile": SAMPLE_PROFILE}
        )
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        before = timezone.now()
        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__public_identifier="bob", campaign=fake_session.campaign)
        assert deal.state == DealState.PENDING
        assert deal.connection_requested_at is not None
        assert deal.connection_requested_at >= before

    @patch("outreach_manager.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_no_stamp_when_already_connected(self, mock_status, mock_strategy, fake_session):
        """Pre-existing CONNECTED status must NOT stamp connection_requested_at."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(
            {"public_identifier": "bob", "url": "https://www.linkedin.com/in/bob/", "profile": SAMPLE_PROFILE}
        )
        mock_status.return_value = ProfileState.CONNECTED

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__public_identifier="bob", campaign=fake_session.campaign)
        assert deal.state == DealState.CONNECTED
        # No stamp — the pre-existing path goes via set_profile_state directly,
        # not via the send-and-stamp branch.
        assert deal.connection_requested_at is None


# ── P3-2 / P3-3: Withdrawal age filter logic ────────────────────────────────


@pytest.mark.django_db
class TestWithdrawalAgeFilter:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def test_uses_connection_requested_at_when_set(self, fake_session):
        """Deals with connection_requested_at > 7 days ago are picked for withdrawal."""
        _make_qualified(fake_session, "charlie")
        set_profile_state(fake_session, "charlie", DealState.PENDING.value)
        # Stamp 10 days ago — should be picked by filter.
        Deal.objects.filter(lead__public_identifier="charlie").update(
            connection_requested_at=timezone.now() - timedelta(days=10),
            # creation_date within 7 days — proves filter uses requested_at, not creation_date
            creation_date=timezone.now() - timedelta(days=2),
        )

        age_filter = _connection_request_age_filter()
        picked = Deal.objects.filter(
            lead__public_identifier="charlie",
            state=DealState.PENDING,
        ).filter(age_filter).exists()

        assert picked, "Deal with old connection_requested_at must be picked even if creation_date is recent"

    def test_recent_connection_requested_at_not_withdrawn(self, fake_session):
        """Deals with connection_requested_at < 7 days ago are NOT picked."""
        _make_qualified(fake_session, "diana")
        set_profile_state(fake_session, "diana", DealState.PENDING.value)
        Deal.objects.filter(lead__public_identifier="diana").update(
            connection_requested_at=timezone.now() - timedelta(days=3),
            creation_date=timezone.now() - timedelta(days=10),
        )

        age_filter = _connection_request_age_filter()
        picked = Deal.objects.filter(
            lead__public_identifier="diana",
            state=DealState.PENDING,
        ).filter(age_filter).exists()

        assert not picked, "Deal with recent connection_requested_at must NOT be picked"

    def test_null_connection_requested_at_not_withdrawn(self, fake_session):
        """Legacy deals with NULL connection_requested_at are NEVER auto-withdrawn.

        creation_date is Deal creation time, not send time — the two must not
        substitute for each other.  A NULL timestamp means we have no reliable
        age information, so these deals are skipped entirely.
        """
        _make_qualified(fake_session, "eve")
        set_profile_state(fake_session, "eve", DealState.PENDING.value)
        Deal.objects.filter(lead__public_identifier="eve").update(
            connection_requested_at=None,
            creation_date=timezone.now() - timedelta(days=10),
        )

        age_filter = _connection_request_age_filter()
        picked = Deal.objects.filter(
            lead__public_identifier="eve",
            state=DealState.PENDING,
        ).filter(age_filter).exists()

        assert not picked, "Deal with NULL connection_requested_at must NOT be auto-withdrawn"


# ── P3-4: UNKNOWN observation does not corrupt Deal state ────────────────────


@pytest.mark.django_db
class TestUnknownStatusPreservesState:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_unknown_targeted_check_leaves_deal_pending(self, mock_status, fake_session):
        """When broad scrape misses the deal and targeted check raises, deal stays PENDING."""
        mock_status.side_effect = Exception("Network error")
        _make_pending_due(fake_session, "frank")

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__public_identifier="frank", campaign=fake_session.campaign)
        # State must remain PENDING — UNKNOWN must never corrupt.
        assert deal.state == DealState.PENDING

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_resolve_status_individually_returns_unknown_on_exception(self, mock_status, fake_session):
        """_resolve_status_individually returns 'UNKNOWN' when get_connection_status raises."""
        mock_status.side_effect = Exception("Network timeout")
        _make_pending_due(fake_session, "george")
        deal = Deal.objects.get(lead__public_identifier="george", campaign=fake_session.campaign)

        result = _resolve_status_individually(fake_session, deal)
        assert result == "UNKNOWN"

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_resolve_status_individually_qualified_returns_unknown(self, mock_status, fake_session):
        """QUALIFIED observed on targeted check → UNKNOWN (ambiguous, not authoritative)."""
        mock_status.return_value = ProfileState.QUALIFIED
        _make_pending_due(fake_session, "helen")
        deal = Deal.objects.get(lead__public_identifier="helen", campaign=fake_session.campaign)

        result = _resolve_status_individually(fake_session, deal)
        assert result == "UNKNOWN"

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_resolve_status_individually_connected_returns_connected(self, mock_status, fake_session):
        """CONNECTED observed on targeted check → 'CONNECTED'."""
        mock_status.return_value = ProfileState.CONNECTED
        _make_pending_due(fake_session, "ivan")
        deal = Deal.objects.get(lead__public_identifier="ivan", campaign=fake_session.campaign)

        result = _resolve_status_individually(fake_session, deal)
        assert result == "CONNECTED"

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_resolve_status_individually_pending_returns_pending(self, mock_status, fake_session):
        """PENDING observed on targeted check → 'PENDING'."""
        mock_status.return_value = ProfileState.PENDING
        _make_pending_due(fake_session, "julia")
        deal = Deal.objects.get(lead__public_identifier="julia", campaign=fake_session.campaign)

        result = _resolve_status_individually(fake_session, deal)
        assert result == "PENDING"


# ── P3-5 / P3-6 / P3-7 / P3-8: Illegal state transition guard ───────────────


@pytest.mark.django_db
class TestIllegalStateTransitionGuard:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def _make_connected(self, session, public_id):
        _make_qualified(session, public_id)
        set_profile_state(session, public_id, DealState.CONNECTED.value)

    def _make_completed(self, session, public_id):
        _make_qualified(session, public_id)
        set_profile_state(session, public_id, DealState.COMPLETED.value)

    def test_connected_to_pending_raises(self, fake_session):
        """CONNECTED → PENDING must raise ValueError."""
        self._make_connected(fake_session, "kate")
        with pytest.raises(ValueError, match="CONNECTED cannot regress to PENDING"):
            set_profile_state(fake_session, "kate", DealState.PENDING.value)

    def test_connected_to_qualified_raises(self, fake_session):
        """CONNECTED → QUALIFIED must raise ValueError."""
        self._make_connected(fake_session, "liam")
        with pytest.raises(ValueError, match="CONNECTED cannot regress to QUALIFIED"):
            set_profile_state(fake_session, "liam", DealState.QUALIFIED.value)

    def test_connected_to_ready_to_connect_raises(self, fake_session):
        """CONNECTED → READY_TO_CONNECT must raise ValueError."""
        self._make_connected(fake_session, "mia")
        with pytest.raises(ValueError, match="CONNECTED cannot regress to READY_TO_CONNECT"):
            set_profile_state(fake_session, "mia", DealState.READY_TO_CONNECT.value)

    def test_completed_to_qualified_raises(self, fake_session):
        """COMPLETED → QUALIFIED must raise ValueError."""
        self._make_completed(fake_session, "noah")
        with pytest.raises(ValueError, match="COMPLETED is terminal"):
            set_profile_state(fake_session, "noah", DealState.QUALIFIED.value)

    def test_completed_to_connected_raises(self, fake_session):
        """COMPLETED → CONNECTED must raise ValueError."""
        self._make_completed(fake_session, "olivia")
        with pytest.raises(ValueError, match="COMPLETED is terminal"):
            set_profile_state(fake_session, "olivia", DealState.CONNECTED.value)

    def test_pending_to_pending_allowed(self, fake_session):
        """PENDING → PENDING (backoff re-stamp) is permitted — same-state re-entry is not illegal."""
        _make_qualified(fake_session, "peter")
        set_profile_state(fake_session, "peter", DealState.PENDING.value)
        # Must not raise.
        set_profile_state(fake_session, "peter", DealState.PENDING.value)
        deal = Deal.objects.get(lead__public_identifier="peter", campaign=fake_session.campaign)
        assert deal.state == DealState.PENDING

    def test_any_state_to_failed_allowed(self, fake_session):
        """CONNECTED → FAILED (permanent failure) is a permitted forward transition."""
        self._make_connected(fake_session, "quinn")
        set_profile_state(fake_session, "quinn", DealState.FAILED.value)
        deal = Deal.objects.get(lead__public_identifier="quinn", campaign=fake_session.campaign)
        assert deal.state == DealState.FAILED

    def test_pending_to_connected_allowed(self, fake_session):
        """PENDING → CONNECTED (acceptance) is the primary forward transition — must be permitted."""
        _make_qualified(fake_session, "rachel")
        set_profile_state(fake_session, "rachel", DealState.PENDING.value)
        set_profile_state(fake_session, "rachel", DealState.CONNECTED.value)
        deal = Deal.objects.get(lead__public_identifier="rachel", campaign=fake_session.campaign)
        assert deal.state == DealState.CONNECTED


# ── P3-9: Broad-scrape CONNECTED path (regression guard) ────────────────────


@pytest.mark.django_db
class TestBroadScrapeConnectedPath:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_broad_scrape_connected_promotes_without_targeted_check(
        self, mock_status, fake_session
    ):
        """If the broad scrape returns the public_id as CONNECTED, no targeted check is needed."""
        # mock_status returns CONNECTED — check_acceptances_page (no-page mode) will add the ID
        mock_status.return_value = ProfileState.CONNECTED
        _make_pending_due(fake_session, "sam")

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__public_identifier="sam", campaign=fake_session.campaign)
        assert deal.state == DealState.CONNECTED
