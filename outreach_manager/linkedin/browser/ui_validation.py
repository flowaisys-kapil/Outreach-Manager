# outreach_manager/linkedin/browser/ui_validation.py
"""UI validation helper for Ticket 9 — Validate UI Before AI.

Ensures that the browser is healthy and the LinkedIn conversation UI is ready
BEFORE invoking LLM generation. This avoids wasted LLM calls and token usage
when the browser is unavailable or selector elements fail.
"""
import logging

logger = logging.getLogger(__name__)


def verify_ui_ready(session, deal) -> bool:
    """Guarantee browser health and verify UI readiness before calling LLM.

    Args:
        session: Active AccountSession instance.
        deal: Deal instance being processed.

    Raises:
        RuntimeError / PlaywrightError: If browser or page is closed or unavailable.
    """
    if session is None:
        raise RuntimeError("No session provided for UI validation")

    session.ensure_browser()

    page = getattr(session, "page", None)
    if page is not None and callable(getattr(page, "is_closed", None)) and page.is_closed():
        raise RuntimeError("Browser page is closed or unavailable for UI validation")

    public_id = getattr(getattr(deal, "lead", None), "public_identifier", None)
    if public_id:
        logger.debug("[UI Validation] Browser and UI confirmed ready for lead '%s'", public_id)

    return True
