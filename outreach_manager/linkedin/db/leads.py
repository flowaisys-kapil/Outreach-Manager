import logging
import random
import time
from typing import Dict, Any, Optional

from django.db import transaction

from linkedin_cli.url_utils import url_to_public_id, public_id_to_url
from outreach_manager.crm.models import DealState

logger = logging.getLogger(__name__)


def lead_exists(url: str) -> bool:
    """Check if Lead already exists for this LinkedIn URL."""
    from outreach_manager.crm.models import Lead

    pid = url_to_public_id(url)
    if not pid:
        return False
    return Lead.objects.filter(public_identifier=pid).exists()


def create_enriched_lead(session, url: str, profile: Dict[str, Any], source_keyword_id: Optional[int] = None) -> Optional[int]:
    """Create Lead with full profile data and embedding.

    Returns lead PK or None if exists.
    Does NOT create Deal — that comes at qualification.
    source_keyword_id: if set, links this lead back to the SearchKeyword that
    found it so the Yield Guard can track qualified-per-keyword ratios.
    """
    from outreach_manager.crm.models import Lead

    # Use canonical public_identifier from Voyager response when available.
    canonical_pid = profile.get("public_identifier")
    public_id = canonical_pid or url_to_public_id(url)
    clean_url = public_id_to_url(public_id)

    urn = profile.get("urn") or None

    with transaction.atomic():
        if Lead.objects.filter(public_identifier=public_id).exists():
            return None
        if urn and Lead.objects.filter(urn=urn).exists():
            logger.info(
                "Lead with URN %s already exists — skipping duplicate %s",
                urn, public_id,
            )
            return None
        country_code = (profile.get("country_code") or "").strip().lower()
        lead = Lead.objects.create(
            linkedin_url=clean_url,
            public_identifier=public_id,
            country_code=country_code,
            source_keyword_id=source_keyword_id,
        )
        _cache_urn_from_profile(lead, profile)

    lead.embed_from_profile(profile)

    logger.debug("Created enriched lead for %s (pk=%d)", public_id, lead.pk)
    return lead.pk


@transaction.atomic
def promote_lead_to_deal(session, public_id: str, reason: str = ""):
    """Create a QUALIFIED Deal for a Lead.

    Returns the Deal.
    """
    from outreach_manager.crm.models import Lead, Deal

    lead = Lead.objects.filter(public_identifier=public_id).first()
    if not lead:
        raise ValueError(f"No Lead for {public_id}")

    deal = Deal.objects.create(
        lead=lead,
        campaign=session.campaign,
        state=DealState.QUALIFIED,
        reason=reason,
    )

    from termcolor import colored
    logger.info("%s %s", public_id, colored("QUALIFIED", "green", attrs=["bold"]))
    return deal


def get_leads_for_qualification(session) -> list:
    """Leads eligible for qualification in the current campaign.

    Returns profile dicts for leads that are not permanently disqualified
    and have no Deal in this campaign.
    """
    from outreach_manager.crm.models import Lead

    # Invariant (convention, not DB-enforced): a disqualified lead is never given
    # a NEW deal. It may still hold a terminal FAILED deal (see connect.py's
    # unreachable path, which disqualifies + FAILs together). Every deal-creating
    # query must filter disqualified=False to uphold this.
    leads = Lead.objects.filter(
        disqualified=False,
    ).exclude(
        deal__campaign=session.campaign,
    )

    return [lead.to_profile_dict() for lead in leads]


def update_lead_slug(old_public_id: str, new_public_id: str):
    """Update a Lead after LinkedIn redirected its vanity URL."""
    from outreach_manager.crm.models import Lead

    new_url = public_id_to_url(new_public_id)
    updated = Lead.objects.filter(public_identifier=old_public_id).update(
        public_identifier=new_public_id,
        linkedin_url=new_url,
    )
    if updated:
        logger.info("Lead slug updated: %s → %s", old_public_id, new_public_id)
    return updated


def disqualify_lead(public_id: str):
    """Set Lead.disqualified = True (account-level, permanent, cross-campaign)."""
    from outreach_manager.crm.models import Lead

    lead = Lead.objects.filter(public_identifier=public_id).first()
    if not lead:
        logger.warning("disqualify_lead: no Lead for %s", public_id)
        return
    lead.disqualified = True
    lead.save(update_fields=["disqualified"])


def discover_and_enrich(session, profiles_or_urls: list, source_keyword_id: Optional[int] = None):
    """Enrich new lead profiles safely without opening profile pages repeatedly.

    Uses zero-navigation search metadata to construct Lead rows, preventing high-volume
    profile view detection on LinkedIn.
    """
    from linkedin_cli.api.client import PlaywrightLinkedinAPI
    from outreach_manager.core.conf import CAMPAIGN_CONFIG

    items = []
    for item in profiles_or_urls:
        if isinstance(item, dict):
            url = item.get("url")
            if url and not lead_exists(url):
                items.append(item)
        elif isinstance(item, str):
            if not lead_exists(item):
                items.append({"url": item})

    if not items:
        return

    max_per_page = CAMPAIGN_CONFIG.get("enrich_max_per_page", 5)
    if len(items) > max_per_page:
        items = items[:max_per_page]

    logger.info("Discovered %d new profile(s) for zero-navigation enrichment", len(items))

    min_delay = CAMPAIGN_CONFIG.get("enrich_min_delay_seconds", 10)
    max_delay = CAMPAIGN_CONFIG.get("enrich_max_delay_seconds", 25)
    api = PlaywrightLinkedinAPI(session=session)
    enriched = 0

    for item in items:
        url = item.get("url")
        public_id = url_to_public_id(url)
        if not public_id:
            continue

        try:
            profile, _raw = api.get_profile(profile_url=url, search_profile=item, navigate=False)
        except Exception as exc:
            logger.warning("Profile extraction failed for %s (%s) — skipping", url, exc)
            continue

        if not profile:
            continue

        if create_enriched_lead(session, url, profile, source_keyword_id=source_keyword_id) is not None:
            enriched += 1

        time.sleep(random.uniform(min_delay, max_delay))

    logger.info("Safely enriched %d/%d new profiles without extra page loads", enriched, len(items))



def _cache_urn_from_profile(lead, profile: Dict[str, Any]):
    """Promote ``profile['urn']`` onto the Lead row if not already cached.

    The only durable field we extract from a fresh scrape -- everything
    else lives in memory for the lifetime of the caller dictionary.
    """
    urn = profile.get("urn") or None
    if urn and lead.urn != urn:
        lead.urn = urn
        lead.save(update_fields=["urn"])


def register_self_lead(session, profile: Dict[str, Any]):
    """Persist the logged-in member own profile as a disqualified Lead.

    The CRM-side layer over linkedin_cli self-discovery primitive: marks
    the real profile disqualified (so auto-discovery never targets it) and links
    it as linkedin_profile.self_lead. Idempotent per profile.
    """
    from outreach_manager.crm.models import Lead

    if not profile or not isinstance(profile, dict):
        return
    public_id = profile.get("public_identifier") or profile.get("public_id")
    if not public_id:
        logger.warning("register_self_lead: profile missing public_identifier: %r", profile)
        return
    lead, _ = Lead.objects.update_or_create(

        public_identifier=public_id,
        defaults={"linkedin_url": public_id_to_url(public_id), "disqualified": True},
    )
    _cache_urn_from_profile(lead, profile)

    session.linkedin_profile.self_lead = lead
    session.linkedin_profile.save(update_fields=["self_lead"])
    logger.info("Registered self-profile as disqualified Lead: %s", public_id)
