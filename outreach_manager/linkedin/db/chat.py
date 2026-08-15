import logging
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass
class ConversationSyncResult:
    """Return value of sync_conversation().

    ``messages``     — the full conversation history as list-of-dicts (source of truth).
    ``new_messages`` — only the ChatMessage rows newly created during *this* sync call.
                       Empty when nothing new arrived; callers must use this to decide
                       whether inbound activity occurred, not ``messages``.
    """
    messages: list = field(default_factory=list)
    new_messages: list = field(default_factory=list)


def _get_lead_and_deal(session, public_identifier: str):
    """Return (lead, deal) for a public identifier in the active campaign."""
    from outreach_manager.crm.models import Deal, Lead

    lead, _ = Lead.objects.get_or_create(public_identifier=public_identifier)
    campaign = getattr(session, "campaign", None)
    deal = (
        Deal.objects.filter(lead=lead, campaign=campaign)
        .select_related("lead")
        .first()
    )
    if deal is None and campaign:
        deal = Deal.objects.create(
            lead=lead,
            campaign=campaign,
            stage="QUALIFIED",
        )
    return lead, deal


_CONVERSATION_URN_CACHE: dict[int, str] = {}


def sync_conversation(
    session,
    public_identifier: str,
    allow_navigation: bool = False,
    conversation_urn: str | None = None,
) -> ConversationSyncResult:
    """Fetch messages from Voyager API and upsert into ChatMessage.

    Returns a ConversationSyncResult with:
      - .messages      — full conversation history as list-of-dicts
      - .new_messages  — ChatMessage ORM objects newly created this call

    Callers that only need to detect new inbound activity MUST use
    ``result.new_messages``, NOT ``result.messages``, to avoid acting on
    stale conversation history from prior cycles.

    Summary generation is secondary: a summary LLM failure is logged as a
    warning but never causes successfully-persisted ChatMessages to be lost.
    """
    lead, deal = _get_lead_and_deal(session, public_identifier)
    if deal is None:
        logger.debug("sync: no deal for %s in %s — skipping", public_identifier, session.campaign)
        return ConversationSyncResult()

    new_messages = _sync_from_api(
        session,
        public_identifier,
        deal,
        allow_navigation=allow_navigation,
        conversation_urn=conversation_urn,
    )

    # Summary update is best-effort: failure must never erase persisted messages.
    try:
        _update_deal_chat_summary(session, deal, new_messages)
    except Exception as exc:
        logger.warning(
            "sync: chat_summary update failed for %s (messages are safe): %s",
            public_identifier, exc,
        )

    return ConversationSyncResult(
        messages=_read_from_db(deal),
        new_messages=new_messages,
    )


def _update_deal_chat_summary(session, deal, new_messages):
    """Fold newly-synced ChatMessages into the campaign Deal's chat_summary."""
    if not new_messages:
        return
    from outreach_manager.core.db.summaries import (
        _build_lead_identity_context, seller_name_from, update_chat_summary,
    )

    update_chat_summary(
        deal,
        new_messages,
        seller_name=seller_name_from(session),
        lead_identity=_build_lead_identity_context(deal),
    )


def _resolve_conversation_urn(
    session,
    api,
    deal,
    target_urn: str,
    mailbox_urn: str,
    allow_navigation: bool,
    explicit_conversation_urn: str | None = None,
) -> str | None:
    """Resolve conversation URN via explicit parameter, memory cache, DB messages, or Voyager API."""
    if explicit_conversation_urn:
        _CONVERSATION_URN_CACHE[deal.pk] = explicit_conversation_urn
        return explicit_conversation_urn

    # 1. Memory cache
    if deal.pk in _CONVERSATION_URN_CACHE:
        return _CONVERSATION_URN_CACHE[deal.pk]

    # 2. Existing ChatMessage records
    from outreach_manager.chat.models import ChatMessage
    existing_msg = ChatMessage.objects.filter(deal=deal, linkedin_urn__contains="fsd_conversation:").first()
    if existing_msg and "fsd_conversation:" in existing_msg.linkedin_urn:
        try:
            conv_part = existing_msg.linkedin_urn.split("fsd_conversation:")[1].split(",")[0].rstrip(")")
            conv_urn = f"urn:li:fsd_conversation:{conv_part}"
            _CONVERSATION_URN_CACHE[deal.pk] = conv_urn
            return conv_urn
        except Exception:
            pass

    # 3. Voyager API GraphQL scan
    from linkedin_cli.actions.conversations import find_conversation_urn, find_conversation_urn_via_navigation
    try:
        conv_urn = find_conversation_urn(api, target_urn, mailbox_urn)
        if conv_urn:
            _CONVERSATION_URN_CACHE[deal.pk] = conv_urn
            return conv_urn
    except Exception as e:
        logger.debug("Voyager find_conversation_urn failed for %s: %s", deal.lead.public_identifier, e)

    # 4. Page navigation fallback (only if explicitly allowed, e.g. single explicit action)
    if allow_navigation:
        try:
            conv_urn = find_conversation_urn_via_navigation(session, target_urn)
            if conv_urn:
                _CONVERSATION_URN_CACHE[deal.pk] = conv_urn
                return conv_urn
        except Exception as e:
            logger.warning("find_conversation_urn_via_navigation failed for %s: %s", deal.lead.public_identifier, e)

    return None


def _sync_from_api(
    session,
    public_identifier: str,
    deal,
    allow_navigation: bool = False,
    conversation_urn: str | None = None,
) -> list:
    """Fetch messages from Voyager API and upsert into DB, scoped to `deal`.

    Returns the list of newly-created ``ChatMessage`` rows (in arrival order),
    so callers can incrementally update derived caches like ``chat_summary``.
    """
    from outreach_manager.chat.models import ChatMessage
    from linkedin_cli.actions.conversations import parse_message_element
    from linkedin_cli.api.client import PlaywrightLinkedinAPI
    from linkedin_cli.api.messaging import fetch_messages

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)

    lead = deal.lead
    target_urn = lead.get_urn(session)
    mailbox_urn = session.self_profile.get("urn", "") if getattr(session, "self_profile", None) else ""
    self_name = session.self_profile.get("full_name", "") if getattr(session, "self_profile", None) else ""

    # Find conversation URN via explicit arg / cache / API / navigation
    conv_urn = _resolve_conversation_urn(
        session, api, deal, target_urn, mailbox_urn, allow_navigation=allow_navigation, explicit_conversation_urn=conversation_urn
    )
    if not conv_urn:
        logger.debug("sync: no conversation URN found for %s", public_identifier)
        return []

    # Fetch messages
    try:
        raw = fetch_messages(api, conv_urn)
    except Exception as e:
        logger.warning("sync: fetch_messages failed for %s: %s", public_identifier, e)
        return []

    elements = raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])
    new_messages: list = []

    for msg in elements:
        parsed = parse_message_element(msg)
        if not parsed or not parsed["entityUrn"]:
            continue

        sender_urn = parsed.get("sender_host_urn", "")
        sender_name = parsed.get("sender_name", "")
        is_outgoing = False
        if mailbox_urn and sender_urn and (mailbox_urn in sender_urn or sender_urn in mailbox_urn):
            is_outgoing = True
        elif self_name and sender_name and self_name.lower() == sender_name.lower():
            is_outgoing = True
        elif sender_name and sender_name.lower() in ("me", "you"):
            is_outgoing = True

        # Upsert by (deal, linkedin_urn): the conversation is per-deal.
        obj, created = ChatMessage.objects.update_or_create(
            deal=deal,
            linkedin_urn=parsed["entityUrn"],
            defaults={
                "content": parsed["text"],
                "is_outgoing": is_outgoing,
                "owner": session.django_user,
                **({f"creation_date": parsed["delivered_at"]} if parsed["delivered_at"] else {}),
            },
        )
        if created:
            new_messages.append(obj)
            logger.debug("sync: new message from %s for %s", parsed["sender_name"], public_identifier)

    # Sort new messages chronologically so the LLM sees them in order.
    new_messages.sort(key=lambda m: m.creation_date or m.pk)
    logger.debug("sync: processed %d messages for %s (%d new)",
                 len(elements), public_identifier, len(new_messages))
    return new_messages


def _read_from_db(deal) -> list[dict]:
    """Read all ChatMessages for a deal, sorted chronologically."""
    from outreach_manager.chat.models import ChatMessage

    lead_name = deal.lead.public_identifier or "them"

    messages = ChatMessage.objects.filter(deal=deal).select_related("owner").order_by("creation_date")

    result = []
    for msg in messages:
        if not msg.content:
            continue
        if msg.is_outgoing:
            owner = msg.owner
            sender = f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else "me"
        else:
            sender = lead_name
        result.append({
            "sender": sender or "me",
            "text": msg.content,
            "timestamp": msg.creation_date.strftime("%Y-%m-%d %H:%M") if msg.creation_date else "",
            "is_outgoing": msg.is_outgoing,
        })
    return result
