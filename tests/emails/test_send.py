# tests/emails/test_send.py
"""The Layer-1 email send path: Mailbox pacing, the eager flush planner,
the email pool query, and the EMAIL task handler."""
import pytest
from unittest.mock import patch

from django.utils import timezone

from outreach_manager.core.agents.email_opener import EmailDraft
from outreach_manager.core.db.deals import get_emailable_deals
from outreach_manager.core.models import Task
from outreach_manager.linkedin.planning import flush_email_queue
from outreach_manager.crm.models import DealState
from outreach_manager.emails.models import Mailbox
from outreach_manager.emails.tasks.send import handle_email
from tests.factories import DealFactory, LeadFactory


def _box(email="a@b.com", daily_limit=10):
    return Mailbox.objects.create(
        username=email, password="pw", from_address=email, daily_limit=daily_limit,
    )


def _ready(campaign, email="lead@corp.com"):
    """A deal queued for its Layer-1 email (READY_TO_EMAIL, address resolved)."""
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(api_email=email),
        state=DealState.READY_TO_EMAIL,
    )


# ── Mailbox pacing ────────────────────────────────────────────────


@pytest.mark.django_db
class TestMailboxPacing:
    def test_sent_today_counts_emailed_deals_for_this_box(self, fake_session):
        box = _box(daily_limit=10)
        d = _ready(fake_session.campaign)
        assert box.sent_today() == 0
        d.mailbox = box
        d.state = DealState.EMAILED
        d.email_sent_at = timezone.now()
        d.save()
        assert box.sent_today() == 1
        assert box.headroom_today() == 9

    def test_remaining_today_sums_headroom_across_boxes(self, fake_session):
        _box("a@b.com", daily_limit=3)
        _box("c@d.com", daily_limit=5)
        assert Mailbox.objects.remaining_today() == 8

    def test_remaining_today_zero_with_no_boxes(self):
        assert Mailbox.objects.remaining_today() == 0

    def test_least_loaded_picks_box_with_most_headroom(self, fake_session):
        light = _box("light@b.com", daily_limit=10)
        heavy = _box("heavy@b.com", daily_limit=10)
        # Spend 4 on heavy.
        for _ in range(4):
            d = _ready(fake_session.campaign)
            d.mailbox = heavy
            d.state = DealState.EMAILED
            d.email_sent_at = timezone.now()
            d.save()
        assert Mailbox.objects.least_loaded_under_cap() == light

    def test_least_loaded_returns_none_when_all_capped(self, fake_session):
        box = _box(daily_limit=1)
        d = _ready(fake_session.campaign)
        d.mailbox = box
        d.state = DealState.EMAILED
        d.email_sent_at = timezone.now()
        d.save()
        assert Mailbox.objects.least_loaded_under_cap() is None


# ── Email pool ────────────────────────────────────────────────────


@pytest.mark.django_db
class TestEmailableDeals:
    def test_returns_only_ready_to_email(self, fake_session):
        ready = _ready(fake_session.campaign)
        DealFactory(campaign=fake_session.campaign, lead=LeadFactory(), state=DealState.QUALIFIED)
        DealFactory(campaign=fake_session.campaign, lead=LeadFactory(), state=DealState.EMAILED)
        deals = list(get_emailable_deals(fake_session))
        assert deals == [ready]

    def test_excludes_disqualified_lead(self, fake_session):
        deal = _ready(fake_session.campaign)
        deal.lead.disqualified = True
        deal.lead.save()
        assert list(get_emailable_deals(fake_session)) == []

    def test_oldest_first(self, fake_session):
        first = _ready(fake_session.campaign, "first@c.com")
        second = _ready(fake_session.campaign, "second@c.com")
        assert list(get_emailable_deals(fake_session)) == [first, second]


# ── flush_email_queue (the eager planner) ─────────────────────────


@pytest.mark.django_db
class TestFlushEmailQueue:
    def _pending_emails(self, campaign):
        return Task.objects.filter(
            task_type=Task.TaskType.EMAIL, payload__campaign_id=campaign.pk,
        ).count()

    def test_no_op_without_a_mailbox(self, fake_session):
        _ready(fake_session.campaign)
        assert flush_email_queue(fake_session, fake_session.campaign) == 0
        assert self._pending_emails(fake_session.campaign) == 0

    def test_no_op_on_empty_pool(self, fake_session):
        _box()
        assert flush_email_queue(fake_session, fake_session.campaign) == 0

    def test_creates_one_slot_per_queued_deal(self, fake_session):
        _box(daily_limit=10)
        _ready(fake_session.campaign, "x@c.com")
        _ready(fake_session.campaign, "y@c.com")
        assert flush_email_queue(fake_session, fake_session.campaign) == 2
        assert self._pending_emails(fake_session.campaign) == 2

    def test_capped_by_pool_headroom(self, fake_session):
        _box(daily_limit=1)
        _ready(fake_session.campaign, "x@c.com")
        _ready(fake_session.campaign, "y@c.com")
        assert flush_email_queue(fake_session, fake_session.campaign) == 1

    def test_no_op_when_email_task_already_pending(self, fake_session):
        _box(daily_limit=10)
        _ready(fake_session.campaign)
        Task.objects.create(
            task_type=Task.TaskType.EMAIL,
            scheduled_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        assert flush_email_queue(fake_session, fake_session.campaign) == 0
        assert self._pending_emails(fake_session.campaign) == 1


# ── handle_email (the EMAIL task) ─────────────────────────────────


@pytest.mark.django_db
class TestHandleEmail:
    def _run(self, fake_session):
        task = Task.objects.create(
            task_type=Task.TaskType.EMAIL,
            scheduled_at=timezone.now(),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        with patch(
            "outreach_manager.core.db.summaries.materialize_profile_summary_if_missing",
        ), patch(
            "outreach_manager.core.agents.email_opener.compose_opener_email",
            return_value=EmailDraft(subject="Hi there", body="Short opener."),
        ), patch(
            "outreach_manager.emails.sender.send_email", return_value="<mid@corp.com>",
        ) as send:
            handle_email(task, fake_session, qualifiers={})
        return send

    def test_sends_and_records_then_moves_to_emailed(self, fake_session):
        box = _box(daily_limit=10)
        deal = _ready(fake_session.campaign, "lead@corp.com")
        send = self._run(fake_session)

        send.assert_called_once_with(box, "lead@corp.com", "Hi there", "Short opener.")
        deal.refresh_from_db()
        assert deal.state == DealState.EMAILED
        assert deal.mailbox == box
        assert deal.email_subject == "Hi there"
        assert deal.email_message_id == "<mid@corp.com>"
        assert deal.email_sent_at is not None

    def test_no_op_when_every_box_is_capped(self, fake_session):
        box = _box(daily_limit=1)
        spent = _ready(fake_session.campaign, "spent@corp.com")
        spent.mailbox = box
        spent.state = DealState.EMAILED
        spent.email_sent_at = timezone.now()
        spent.save()
        queued = _ready(fake_session.campaign, "queued@corp.com")

        send = self._run(fake_session)
        send.assert_not_called()
        queued.refresh_from_db()
        assert queued.state == DealState.READY_TO_EMAIL
