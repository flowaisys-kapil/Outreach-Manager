import logging

import numpy as np
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


class Lead(models.Model):
    class Meta:
        verbose_name = _("Lead")
        verbose_name_plural = _("Leads")

    linkedin_url = models.URLField(max_length=200, unique=True)
    public_identifier = models.CharField(max_length=200, unique=True)
    urn = models.CharField(max_length=200, null=True, blank=True, unique=True, db_index=True)
    # ISO-3166 alpha-2 of the lead's current location, cached from the profile
    # scrape. Drives the contacts-store geo-gate; blank = unknown (→ never contributed).
    country_code = models.CharField(max_length=2, blank=True, default="")
    embedding = models.BinaryField(null=True, blank=True)
    # Email enrichment — one field per source (roadmap: p1-e1 storage decision):
    #   contact_info — raw LinkedIn contact-info overlay {email, emails, phone_numbers},
    #                  captured once at CONNECTED; null = never scraped (idempotency flag).
    #   api_email    — enrichment-API result (BetterContact); its writer lands with the
    #                  finder slice (p1-e3). null = not found.
    contact_info = models.JSONField(null=True, blank=True, default=None)
    api_email = models.EmailField(null=True, blank=True, default=None)
    disqualified = models.BooleanField(default=False)
    
    # LLM qualification caching layer fields
    llm_is_qualified = models.BooleanField(null=True, blank=True, default=None)
    llm_qualification_reason = models.TextField(blank=True, default="")
    llm_qualification_hash = models.CharField(max_length=64, blank=True, default="")

    # Yield Guard: which SearchKeyword discovered this lead
    source_keyword = models.ForeignKey(
        "linkedin.SearchKeyword",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="discovered_leads",
    )

    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        label = self.public_identifier or self.linkedin_url or f"Lead#{self.pk}"
        if self.disqualified:
            return f"({_('Disqualified')}) {label}"
        return label

    # ------------------------------------------------------------------
    # Lazy accessors — re-scrape live on demand, persist only the
    # derived caches we still keep (urn, embedding).
    # ------------------------------------------------------------------

    def get_profile(self, session) -> dict | None:
        """Live Voyager scrape of the parsed profile dict.

        No DB caching: the heavy fields (raw JSON, names, company) live
        only in memory for as long as the caller holds the dict. We do
        opportunistically populate ``self.urn`` if it's still null and
        the scrape returns one.
        """
        from linkedin_cli.api.client import PlaywrightLinkedinAPI
        from linkedin_cli.exceptions import ProfileInaccessibleError

        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        try:
            profile, _raw = api.get_profile(public_identifier=self.public_identifier)
        except ProfileInaccessibleError:
            return None
        if not profile:
            return None

        canonical_pid = profile.get("public_identifier")
        if canonical_pid and self.public_identifier != canonical_pid:
            # Check if a duplicate lead already exists with the new public identifier
            dup = Lead.objects.filter(public_identifier=canonical_pid).exclude(pk=self.pk).first()
            if dup:
                logger.warning(
                    "Lead slug redirection: %s -> %s, but duplicate exists pk=%d. Merging.",
                    self.public_identifier, canonical_pid, dup.pk
                )
                from outreach_manager.crm.models import Deal
                Deal.objects.filter(lead=self).update(lead=dup)
                self.delete()
                # Return profile from the duplicate
                return profile
            else:
                logger.info(
                    "Lead slug redirection: updating %s -> %s",
                    self.public_identifier, canonical_pid
                )
                self.public_identifier = canonical_pid
                from linkedin_cli.url_utils import public_id_to_url
                self.linkedin_url = public_id_to_url(canonical_pid)
                self.save(update_fields=["public_identifier", "linkedin_url"])

        urn = profile.get("urn") or None
        if urn and self.urn != urn:
            if Lead.objects.filter(urn=urn).exclude(pk=self.pk).exists():
                logger.warning("URN %s already owned by another lead — skipping for %s", urn, self.public_identifier)
            else:
                self.urn = urn
                self.save(update_fields=["urn"])

        country = (profile.get("country_code") or "").strip().lower()
        if country and self.country_code != country:
            self.country_code = country
            self.save(update_fields=["country_code"])

        return profile

    def capture_contact_info(self, session) -> None:
        """Scrape + persist the LinkedIn contact-info overlay for a 1st-degree
        connection.

        The stored value is a tri-state retry sentinel: ``None`` means we never
        got a clean read (never tried, or the fetch raised — the error path
        returns before the field is written), so a later visit retries; a non-null
        overlay — even an email-empty ``{email: None, emails: [], phone_numbers:
        []}`` — means the read succeeded and the member simply exposes no email,
        so it is not re-scraped. The raw overlay is stored unfiltered
        (work-vs-personal cleaning is downstream, in dbt).

        Errors are left to the caller: capture is driven from the CONNECTED
        transition (``set_profile_state``) and from each follow-up visit, both of
        which own the best-effort guard (``ProfileInaccessibleError``/``IOError``
        swallowed → field stays ``None`` → retried; ``AuthenticationError``
        propagates to the daemon's reauth handler).
        """
        if self.contact_info is not None:
            return
        from linkedin_cli.api.client import PlaywrightLinkedinAPI

        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        contact, _raw = api.get_contact_info(public_identifier=self.public_identifier)
        self.contact_info = contact
        self.save(update_fields=["contact_info"])

    def resolve_api_email(self) -> bool | None:
        """Resolve + persist a work email via BetterContact, once the lead qualifies.

        Returns True on a hit (``api_email`` set, cached — never re-resolved →
        caller routes the Deal QUALIFIED → READY_TO_EMAIL), False on a genuine
        miss (BetterContact ran, found nothing → caller leaves the Deal QUALIFIED
        for the connect leg), and None when it couldn't run (no key, or the
        service was unreachable → caller leaves the Deal QUALIFIED to retry).
        A miss is free to retry — BetterContact bills only usable hits.
        """
        if self.api_email:
            return True
        from outreach_manager.emails.bettercontact import (
            BetterContactQuery,
            BetterContactUnavailable,
            resolve_email,
        )

        try:
            result = resolve_email(BetterContactQuery(linkedin_url=self.linkedin_url))
        except BetterContactUnavailable:
            return None
        if result:
            self.api_email = result.email
            self.save(update_fields=["api_email"])
            return True
        return False

    def get_urn(self, session) -> str:
        """LinkedIn URN. Reads cached column; falls back to a live scrape."""
        if self.urn:
            return self.urn
        self.get_profile(session)  # sets self.urn as side-effect
        if self.urn:
            return self.urn
        raise ValueError(f"Lead {self.pk}: could not resolve URN after re-fetch")

    def get_embedding(self, session) -> np.ndarray | None:
        """384-dim embedding. Lazy: scrapes + embeds on first access."""
        if self.embedding is None:
            profile = self.get_profile(session)
            if profile:
                self.embed_from_profile(profile)
        return self.embedding_array

    def embed_from_profile(self, profile: dict) -> None:
        """Compute and persist the 384-dim embedding from an in-hand profile.

        Used by callers that already have a freshly parsed profile dict,
        so they can skip the scrape that ``get_embedding`` would trigger.
        """
        from outreach_manager.linkedin.ml.embeddings import embed_text
        from outreach_manager.linkedin.ml.profile_text import build_profile_text

        text = build_profile_text({"profile": profile})
        emb = embed_text(text)
        self.embedding = emb.tobytes()
        self.save(update_fields=["embedding"])

    def to_profile_dict(self) -> dict:
        """Standard profile dict shape used by qualifiers and pools.

        The ``profile`` key is intentionally absent — callers that need
        the full Voyager-parsed dict must call ``get_profile(session)``
        themselves (live scrape).

        ``urn`` is included here so the connection-send library can address the
        member directly without an extra profile-page round-trip.
        """
        return {
            "lead_id": self.pk,
            "public_identifier": self.public_identifier,
            "url": self.linkedin_url or "",
            "urn": self.urn or "",
            "meta": {},
        }

    @property
    def embedding_array(self) -> np.ndarray | None:
        """384-dim float32 numpy array from stored bytes, or None."""
        if self.embedding is None:
            return None
        return np.frombuffer(bytes(self.embedding), dtype=np.float32).copy()

    @embedding_array.setter
    def embedding_array(self, arr: np.ndarray):
        self.embedding = np.asarray(arr, dtype=np.float32).tobytes()

    @classmethod
    def get_labeled_arrays(cls, campaign) -> tuple[np.ndarray, np.ndarray]:
        """Labeled embeddings for a campaign as (X, y) numpy arrays for warm start.

        Labels are derived from Deal state and outcome:
        - label=1: Deals at any non-FAILED state (QUALIFIED and beyond)
        - label=0: FAILED Deals with outcome "wrong_fit" (LLM rejection)
        - Skipped: FAILED Deals with other outcomes (operational failures)
        """
        from outreach_manager.crm.models import Outcome
        from outreach_manager.crm.models.deal import Deal
        from outreach_manager.crm.models import DealState

        deals = Deal.objects.filter(
            campaign=campaign, lead_id__isnull=False,
        ).values_list("lead_id", "state", "outcome")

        label_by_lead: dict[int, int] = {}
        for lid, state, outcome in deals:
            if state == DealState.FAILED:
                if outcome == Outcome.WRONG_FIT:
                    label_by_lead[lid] = 0
            else:
                label_by_lead[lid] = 1

        if not label_by_lead:
            return np.empty((0, 384), dtype=np.float32), np.empty(0, dtype=np.int32)

        leads_with_emb = dict(
            cls.objects.filter(pk__in=label_by_lead, embedding__isnull=False)
            .values_list("pk", "embedding")
        )

        X_list, y_list = [], []
        for lid, label in label_by_lead.items():
            emb = leads_with_emb.get(lid)
            if emb is None:
                continue
            X_list.append(np.frombuffer(bytes(emb), dtype=np.float32))
            y_list.append(label)

        if not X_list:
            return np.empty((0, 384), dtype=np.float32), np.empty(0, dtype=np.int32)

        return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.int32)

    def merge_with(self, other_lead):
        """Merge another lead into this lead (self is the master, other_lead is merged and deleted)."""
        if self.pk == other_lead.pk:
            return

        logger.info("Merging duplicate lead %s into master %s", other_lead.public_identifier, self.public_identifier)

        # 1. Update URN, country_code, contact_info, api_email if they are set on other_lead but not on self
        updated = False
        if not self.urn and other_lead.urn:
            self.urn = other_lead.urn
            updated = True
        if not self.country_code and other_lead.country_code:
            self.country_code = other_lead.country_code
            updated = True
        if self.contact_info is None and other_lead.contact_info is not None:
            self.contact_info = other_lead.contact_info
            updated = True
        if not self.api_email and other_lead.api_email:
            self.api_email = other_lead.api_email
            updated = True
        if updated:
            self.save()

        # 2. Re-associate LinkedInProfile self_lead
        from outreach_manager.linkedin.models import LinkedInProfile
        LinkedInProfile.objects.filter(self_lead=other_lead).update(self_lead=self)

        # 3. Process Deals
        from outreach_manager.crm.models.deal import Deal
        from outreach_manager.chat.models import ChatMessage

        for deal in Deal.objects.filter(lead=other_lead):
            # Check if there is already a deal for self in the same campaign
            master_deal = Deal.objects.filter(lead=self, campaign=deal.campaign).first()
            if master_deal:
                # Merge deal details
                # Move chat messages
                ChatMessage.objects.filter(deal=deal).update(deal=master_deal)
                # Adopt the duplicate's state/mailbox if more advanced
                from outreach_manager.crm.models import DealState
                state_rank = {
                    DealState.QUALIFIED: 0,
                    DealState.READY_TO_CONNECT: 1,
                    DealState.PENDING: 2,
                    DealState.CONNECTED: 3,
                    DealState.READY_TO_EMAIL: 4,
                    DealState.EMAILED: 5,
                    DealState.COMPLETED: 6,
                    DealState.FAILED: 7,
                }
                curr_rank = state_rank.get(master_deal.state, -1)
                other_rank = state_rank.get(deal.state, -1)
                if other_rank > curr_rank:
                    master_deal.state = deal.state
                    master_deal.mailbox = deal.mailbox
                    master_deal.save(update_fields=["state", "mailbox"])
                # Delete duplicate deal
                deal.delete()
            else:
                # Re-associate deal to master lead
                deal.lead = self
                deal.save(update_fields=["lead"])

        # 4. Delete the duplicate lead
        other_lead.delete()

    @classmethod
    def perform_deduplication(cls):
        """Find and merge all duplicate Lead profiles in the database."""
        from django.db.models import Count
        
        # 1. Deduplicate by URN
        duplicate_urns = [
            item["urn"] for item in cls.objects.values("urn").annotate(c=Count("id")).filter(c__gt=1)
            if item["urn"]
        ]
        for urn in duplicate_urns:
            leads = list(cls.objects.filter(urn=urn).order_by("id"))
            if len(leads) > 1:
                master = leads[0]
                for duplicate in leads[1:]:
                    master.merge_with(duplicate)

        # 2. Deduplicate by api_email
        duplicate_emails = [
            item["api_email"] for item in cls.objects.values("api_email").annotate(c=Count("id")).filter(c__gt=1)
            if item["api_email"]
        ]
        for email in duplicate_emails:
            leads = list(cls.objects.filter(api_email=email).order_by("id"))
            if len(leads) > 1:
                master = leads[0]
                for duplicate in leads[1:]:
                    master.merge_with(duplicate)

        # 3. Deduplicate by emails inside contact_info JSON
        all_leads = cls.objects.exclude(contact_info=None)
        email_to_leads = {}
        for lead in all_leads:
            info = lead.contact_info or {}
            emails = set()
            if info.get("email"):
                emails.add(info["email"].lower().strip())
            for e in info.get("emails") or []:
                if e:
                    emails.add(e.lower().strip())
            
            for email in emails:
                email_to_leads.setdefault(email, []).append(lead)

        for email, leads in email_to_leads.items():
            if len(leads) > 1:
                try:
                    master = cls.objects.get(pk=leads[0].pk)
                except cls.DoesNotExist:
                    continue
                for lead in leads[1:]:
                    try:
                        duplicate = cls.objects.get(pk=lead.pk)
                        master.merge_with(duplicate)
                    except cls.DoesNotExist:
                        continue
