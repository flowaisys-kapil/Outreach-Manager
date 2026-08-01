import logging

from django.db import transaction
from termcolor import colored

from outreach_manager.crm.models import DealState

logger = logging.getLogger(__name__)

_STATE_LOG_STYLE = {
    DealState.QUALIFIED: ("QUALIFIED", "green", []),
    DealState.READY_TO_CONNECT: ("READY_TO_CONNECT", "yellow", ["bold"]),
    DealState.READY_TO_EMAIL: ("READY_TO_EMAIL", "blue", ["bold"]),
    DealState.EMAILED: ("EMAILED", "blue", []),
    DealState.PENDING: ("PENDING", "cyan", []),
    DealState.CONNECTED: ("CONNECTED", "green", ["bold"]),
    DealState.COMPLETED: ("COMPLETED", "green", ["bold"]),
    DealState.FAILED: ("FAILED", "red", ["bold"]),
    DealState.LEAD: ("LEAD", "cyan", []),
    DealState.RESPONDED: ("RESPONDED", "yellow", ["bold"]),
    DealState.NURTURING: ("NURTURING", "yellow", []),
    DealState.MEETING_SCHEDULED: ("MEETING_SCHEDULED", "magenta", ["bold"]),
    DealState.CLOSED_WON: ("CLOSED_WON", "green", ["bold"]),
    DealState.CLOSED_LOST: ("CLOSED_LOST", "red", ["bold"]),
}

# Illegal state transitions: maps (from_state, to_state) → reason.
# These are transitions that are semantically impossible and would silently
# corrupt lifecycle state if allowed through.  set_profile_state raises
# ValueError on any of these rather than writing the bad transition.
#
# Permitted same-state re-entry (e.g., PENDING→PENDING for backoff doubling)
# and all forward transitions not listed here are allowed through.
_ILLEGAL_TRANSITIONS: dict[tuple[DealState, DealState], str] = {
    # A confirmed connection cannot regress to an earlier funnel state.
    (DealState.CONNECTED, DealState.PENDING):          "CONNECTED cannot regress to PENDING",
    (DealState.CONNECTED, DealState.QUALIFIED):        "CONNECTED cannot regress to QUALIFIED",
    (DealState.CONNECTED, DealState.READY_TO_CONNECT): "CONNECTED cannot regress to READY_TO_CONNECT",
    # Terminal states must not be re-opened by automated code.
    (DealState.COMPLETED, DealState.QUALIFIED):        "COMPLETED is terminal — cannot reopen to QUALIFIED",
    (DealState.COMPLETED, DealState.CONNECTED):        "COMPLETED is terminal — cannot reopen to CONNECTED",
    (DealState.CLOSED_WON, DealState.QUALIFIED):       "CLOSED_WON is terminal — cannot reopen",
    (DealState.CLOSED_LOST, DealState.QUALIFIED):      "CLOSED_LOST is terminal — cannot reopen",
}



def increment_connect_attempts(session, public_id: str) -> int:
    """Increment connect_attempts on the Deal and return the new count."""
    from outreach_manager.crm.models import Deal

    deal = Deal.objects.filter(
        lead__public_identifier=public_id, campaign=session.campaign,
    ).first()
    if not deal:
        return 1

    deal.connect_attempts += 1
    deal.save(update_fields=["connect_attempts"])
    return deal.connect_attempts


def _deal_to_profile_dict(deal) -> dict:
    """Convert a Deal (with select_related lead) to a profile dict for lanes."""
    base = deal.lead.to_profile_dict()
    base["meta"] = {
        "connect_attempts": deal.connect_attempts,
        "backoff_hours": deal.backoff_hours,
        "reason": deal.reason,
    }
    return base


def _deals_at_state(session, state: DealState) -> list:
    """Return profile dicts for all Deals at the given state in this campaign."""
    from outreach_manager.crm.models import Deal

    qs = Deal.objects.filter(
        state=state,
        campaign=session.campaign,
    ).select_related("lead")
    return [_deal_to_profile_dict(d) for d in qs]


def _existing_deal_or_lead(public_id: str, campaign):
    """Check for an existing Deal in campaign; if none, look up the Lead.

    Returns (lead, existing_deal) — exactly one will be non-None,
    or both None if no Lead exists at all.
    """
    from outreach_manager.crm.models import Deal, Lead

    existing = Deal.objects.filter(lead__public_identifier=public_id, campaign=campaign).first()
    if existing:
        return None, existing
    lead = Lead.objects.filter(public_identifier=public_id).first()
    return lead, None


# ── State transitions ──


def capture_and_contribute(lead, session) -> None:
    """Best-effort LinkedIn contact-info capture + contribution for a connection.

    Fired on the CONNECTED transition and on each follow-up visit, but the
    contribution rides the ``Lead.contact_info`` null→non-null transition: it is
    given to the central store **only on the visit that first captures the
    overlay**. ``Lead.capture_contact_info`` is write-once (skips the scrape once
    a value is stored), so a later visit finds the field already set and no-ops —
    no re-sending the same source to the append-only hub log on every follow-up.
    While the scrape keeps failing the field stays null, so capture (and the
    contribution) is retried on the next visit. A failure here must never roll
    back the transition or fail the task, so expected scrape/network errors are
    swallowed with a log; ``AuthenticationError`` still propagates (the daemon's
    reauth handler owns it, and capture is moot on a dead session).
    """
    from linkedin_cli.exceptions import ProfileInaccessibleError
    from outreach_manager.contacts import service as contacts

    fresh_capture = lead.contact_info is None
    try:
        lead.capture_contact_info(session)
    except (ProfileInaccessibleError, IOError) as exc:
        logger.warning("contact-info capture failed for %s: %s", lead.public_identifier, exc)
        return

    if not fresh_capture:
        return  # overlay already captured + contributed on an earlier visit

    emails = (lead.contact_info or {}).get("emails") or []
    logger.debug("contact-info captured for %s: %d email(s)", lead.public_identifier, len(emails))
    contacts.contribute(session, lead, emails, contacts.ORIGIN_PROFILE_INFO)


def set_profile_state(session, public_identifier: str, new_state: str, reason: str = "", outcome: str = ""):
    """Move the Deal to the corresponding state and enqueue the implied next task.

    Campaign-scoped: only finds Deals in the current campaign.
    Raises ValueError if no Deal exists.

    Task creation for state-driven transitions (CONNECTED → follow_up,
    PENDING → check_pending) happens here via the scheduler hook — callers
    do not enqueue directly.
    """
    from outreach_manager.crm.models import Deal
    from outreach_manager.linkedin.scheduler import on_deal_state_entered

    deal = (
        Deal.objects.filter(lead__public_identifier=public_identifier, campaign=session.campaign)
        .select_related("lead")
        .first()
    )
    if not deal:
        raise ValueError(f"No Deal for {public_identifier} — cannot set state {new_state}")

    ps = DealState(new_state)
    state_changed = (deal.state != ps)

    # Illegal-transition guard — raise before touching the DB.
    illegal_reason = _ILLEGAL_TRANSITIONS.get((deal.state, ps))
    if illegal_reason:
        raise ValueError(
            f"Illegal state transition {deal.state} → {ps} for {public_identifier}: {illegal_reason}"
        )

    deal.state = ps

    if reason:
        deal.reason = reason
    if outcome:
        deal.outcome = outcome

    deal.save()

    label, color, attrs = _STATE_LOG_STYLE.get(ps, ("ERROR", "red", ["bold"]))
    suffix = f" ({reason})" if reason else ""
    if state_changed:
        logger.info("%s %s%s", public_identifier, colored(label, color, attrs=attrs), suffix)
    else:
        logger.debug("%s %s (unchanged)%s", public_identifier, label, suffix)

    on_deal_state_entered(deal)

    if state_changed and ps == DealState.CONNECTED:
        capture_and_contribute(deal.lead, session)
        try:
            from outreach_manager.crm.models.event_log import EventLog
            EventLog.objects.create(
                campaign=session.campaign,
                deal=deal,
                event_type=EventLog.EventType.CONNECT_ACCEPTED,
                detail=f"{public_identifier} accepted the connection request."
            )
        except Exception as e:
            logger.warning("Failed to log connect_accepted event: %s", e)


# ── State queries ──


def get_qualified_profiles(session) -> list:
    """Connect-eligible QUALIFIED deals — those WITHOUT a resolved email
    and not currently in withdrawal cooldown.
    """
    from outreach_manager.crm.models import Deal
    from django.db.models import Q
    from django.utils import timezone
    from datetime import timedelta

    cooldown_cutoff = timezone.now() - timedelta(days=21)

    qs = Deal.objects.filter(
        state=DealState.QUALIFIED,
        campaign=session.campaign,
        lead__api_email__isnull=True,
    ).filter(
        Q(last_withdrawn_at__isnull=True) | Q(last_withdrawn_at__lte=cooldown_cutoff)
    ).select_related("lead")
    return [_deal_to_profile_dict(d) for d in qs]


def get_ready_to_connect_profiles(session) -> list:
    return _deals_at_state(session, DealState.READY_TO_CONNECT)


def get_emailable_deals(session):
    """The email pool — Deals queued for their single Layer-1 email, oldest first.

    Symmetric with the connect pools above: each reads exactly one FSM state. The
    state alone is the eligibility — the qualify router reaches READY_TO_EMAIL only
    on a finder hit (so ``Lead.api_email`` is set), and the send moves it to EMAILED
    (so it is never-emailed). Returns ``Deal`` rows (not profile dicts — the EMAIL
    task acts on the Deal directly). ``disqualified`` guards a post-qualification
    do-not-contact, matching the follow_up pool.
    """
    from outreach_manager.crm.models import Deal

    return (
        Deal.objects.filter(
            campaign=session.campaign,
            state=DealState.READY_TO_EMAIL,
            lead__disqualified=False,
        )
        .select_related("lead", "mailbox")
        .order_by("creation_date")
    )


def get_profile_dict_for_public_id(session, public_id: str) -> dict | None:
    """Load profile dict for a single public_id from Deal + Lead (campaign-scoped)."""
    from outreach_manager.crm.models import Deal

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead")
        .first()
    )
    if not deal:
        return None
    return _deal_to_profile_dict(deal)


# ── Deal creation ──


@transaction.atomic
def create_disqualified_deal(session, public_id: str, reason: str = ""):
    """Create a FAILED Deal with 'Disqualified' closing reason for an LLM-rejected lead.

    LLM qualification rejections are tracked as FAILED Deals (campaign-scoped),
    NOT as Lead.disqualified (which is for permanent account-level exclusion).
    """
    from outreach_manager.crm.models import Outcome

    campaign = session.campaign
    lead, existing = _existing_deal_or_lead(public_id, campaign)
    if existing:
        return existing
    if not lead:
        logger.warning("create_disqualified_deal: no Lead for %s", public_id)
        return None

    deal = _create_deal(
        lead=lead,
        state=DealState.FAILED,
        session=session,
        outcome=Outcome.WRONG_FIT,
        reason=reason,
    )

    suffix = f" ({reason})" if reason else ""
    logger.info("%s %s%s", public_id, colored("DISQUALIFIED", "red", attrs=["bold"]), suffix)
    return deal


# create_freemium_deal removed as freemium is disabled.


def _create_deal(
    *, lead, state, session,
    outcome="", reason="",
):
    """Shared Deal creation with common defaults."""
    from outreach_manager.crm.models import Deal

    return Deal.objects.create(
        lead=lead,
        campaign=session.campaign,
        state=state,
        outcome=outcome,
        reason=reason,
    )
