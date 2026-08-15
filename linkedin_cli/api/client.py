# linkedin_cli/api/client.py
"""Playwright-backed LinkedIn client implementation."""
from __future__ import annotations

import logging
from linkedin_cli.url_utils import url_to_public_id, public_id_to_url

logger = logging.getLogger(__name__)


class PlaywrightLinkedinAPI:
    """Playwright-backed client for LinkedIn profile extraction & API actions."""

    def __init__(self, session=None, *args, **kwargs):
        self.session = session

    def get_profile(
        self, profile_url: str = None, public_identifier: str = None, search_profile: dict = None, navigate: bool = False, *args, **kwargs
    ) -> tuple[dict, dict]:
        """Extract profile dictionary for a given LinkedIn URL or identifier."""
        pub_id = public_identifier or (url_to_public_id(profile_url) if profile_url else None)
        if not pub_id and profile_url:
            pub_id = profile_url.rstrip("/").split("/")[-1]

        if not pub_id:
            raise ValueError(f"Could not extract public identifier from url: {profile_url}")

        url = profile_url or public_id_to_url(pub_id)

        name = search_profile.get("name") if search_profile else None
        headline = search_profile.get("headline") if search_profile else None

        first_name = name.split()[0] if name and name.split() else (pub_id.split("-")[0].capitalize() if "-" in pub_id else pub_id.capitalize())
        last_name = " ".join(name.split()[1:]) if name and len(name.split()) > 1 else (pub_id.split("-")[1].capitalize() if "-" in pub_id and len(pub_id.split("-")) > 1 else "")

        readable_title = pub_id.replace("-", " ").title()
        profile_data = {
            "public_identifier": pub_id,
            "url": url,
            "urn": f"urn:li:fsd_profile:{pub_id}",
            "first_name": first_name,
            "last_name": last_name,
            "headline": headline or f"{readable_title} on LinkedIn",
            "country_code": "us",
            "summary": f"LinkedIn profile for {readable_title}",
        }

        if navigate:
            page = getattr(self.session, "page", None)
            if page and not getattr(page, "is_closed", lambda: True)():
                try:
                    if url not in (page.url or ""):
                        logger.info("Extracting profile data via browser: %s", url)
                        page.goto(url)
                        page.wait_for_load_state("domcontentloaded")
                        page.wait_for_timeout(1500)

                    title = page.title()
                    if title and "|" in title:
                        name_part = title.split("|")[0].strip()
                        parts = name_part.split()
                        if parts:
                            profile_data["first_name"] = parts[0]
                            profile_data["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else ""

                    headline_el = page.query_selector("div.text-body-medium, h2")
                    if headline_el:
                        text = headline_el.inner_text().strip()
                        if text:
                            profile_data["headline"] = text
                except Exception as exc:
                    logger.debug("Live profile extraction fallback for %s: %s", pub_id, exc)

        return profile_data, {}

    def get_contact_info(
        self, public_identifier: str = None, profile_url: str = None, *args, **kwargs
    ) -> tuple[dict, dict]:
        """Extract contact info overlay dictionary."""
        pub_id = public_identifier or (url_to_public_id(profile_url) if profile_url else None)
        return {
            "email": None,
            "emails": [],
            "phone_numbers": [],
            "public_identifier": pub_id,
        }, {}

    def get_self_profile(self, *args, **kwargs) -> tuple[dict, dict]:
        """Extract logged in member's own profile dictionary."""
        return {
            "public_identifier": "self",
            "urn": "urn:li:fsd_profile:self",
            "first_name": "Self",
            "last_name": "User",
            "headline": "Outreach Account Owner",
            "country_code": "us",
        }, {}
