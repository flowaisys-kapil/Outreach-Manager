# linkedin_cli/actions/status.py
"""LinkedIn Status Action Handler."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class StatusResult:
    def __init__(self, value: str):
        self.value = value


def get_connection_status(session, profile, *args, **kwargs) -> StatusResult:
    """Inspect current connection status on LinkedIn profile page."""
    try:
        page = getattr(session, "page", session)
        if not page or getattr(page, "is_closed", lambda: True)():
            return StatusResult("Qualified")

        url = profile.get("url") if isinstance(profile, dict) else getattr(profile, "url", None)
        if not url and hasattr(profile, "public_identifier"):
            url = f"https://www.linkedin.com/in/{profile.public_identifier}/"

        if url:
            page.goto(url)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1500)

            content = page.content().lower()
            if "pending" in content or page.query_selector("button:has-text('Pending')"):
                return StatusResult("Pending")
            if "1st" in content or page.query_selector("button:has-text('Message')"):
                return StatusResult("Connected")

        return StatusResult("Qualified")
    except Exception as exc:
        logger.warning("get_connection_status error: %s", exc)
        return StatusResult("Qualified")
