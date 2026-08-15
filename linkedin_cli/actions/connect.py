# linkedin_cli/actions/connect.py
"""LinkedIn Connect Action Handler."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ConnectResult:
    def __init__(self, value: str):
        self.value = value


def send_connection_request(session, profile, *args, **kwargs) -> ConnectResult:
    """Send connection request on LinkedIn profile page."""
    try:
        page = getattr(session, "page", session)
        if not page or getattr(page, "is_closed", lambda: True)():
            return ConnectResult("Qualified")

        url = profile.get("url") if isinstance(profile, dict) else getattr(profile, "url", None)
        if not url and hasattr(profile, "public_identifier"):
            url = f"https://www.linkedin.com/in/{profile.public_identifier}/"

        if url:
            logger.info("Navigating to profile to send connect request: %s", url)
            page.goto(url)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)

            # 1. Check if already connected or pending
            content = page.content().lower()
            if "pending" in content or page.query_selector("button:has-text('Pending')"):
                logger.info("Profile %s is already Pending", url)
                return ConnectResult("Pending")
            if "message" in content and page.query_selector("button:has-text('Message')"):
                logger.info("Profile %s is already Connected", url)
                return ConnectResult("Connected")

            # 2. Look for direct Connect button
            connect_btn = page.query_selector("button:has-text('Connect'), button[aria-label*='Invite']")
            if not connect_btn:
                # 3. Look inside 'More' dropdown menu
                more_btn = page.query_selector("button:has-text('More'), button[aria-label='More actions']")
                if more_btn:
                    more_btn.click()
                    page.wait_for_timeout(1000)
                    connect_btn = page.query_selector("button:has-text('Connect'), span:has-text('Connect')")

            if connect_btn:
                connect_btn.click()
                page.wait_for_timeout(1500)

                # Send without a note if modal pops up
                send_now = page.query_selector("button:has-text('Send without a note'), button[aria-label='Send without a note'], button:has-text('Send now')")
                if send_now:
                    send_now.click()
                    page.wait_for_timeout(1500)

                logger.info("Successfully sent connection request to %s", url)
                return ConnectResult("Pending")

        logger.warning("No connect button found on profile: %s", url)
        return ConnectResult("Qualified")
    except Exception as exc:
        logger.warning("send_connection_request error: %s", exc)
        return ConnectResult("Qualified")
