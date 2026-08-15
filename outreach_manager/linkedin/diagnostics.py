# openoutreach/linkedin/diagnostics.py
"""Capture page state on automation failures for post-mortem debugging."""
from __future__ import annotations

import logging
import traceback
from contextlib import contextmanager
from datetime import datetime

from outreach_manager.core.conf import DIAGNOSTICS_DIR

logger = logging.getLogger(__name__)


def capture_failure(session, error: BaseException) -> str:
    """Save page HTML, screenshot, and error details into a per-failure folder."""
    from pathlib import Path
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    error_name = type(error).__name__
    folder = DIAGNOSTICS_DIR / f"{timestamp}_{error_name}"
    folder.mkdir(parents=True, exist_ok=True)

    if hasattr(session, "diagnostics_generated"):
        session.diagnostics_generated += 1

    # Error traceback
    tb = traceback.format_exception(type(error), error, error.__traceback__)
    (folder / "error.txt").write_text("".join(tb))

    page = getattr(session, "page", None)
    if page is not None and not page.is_closed():
        try:
            (folder / "page.html").write_text(page.content())
        except Exception as exc:
            logger.debug("Failed to capture HTML: %s", exc)

        try:
            page.screenshot(path=str(folder / "screenshot.png"))
        except Exception as exc:
            logger.debug("Failed to capture screenshot: %s", exc)
    else:
        (folder / "page.html").write_text("<!-- page was None or closed -->")

    try:
        rel_path = str(folder.relative_to(Path.cwd()))
    except Exception:
        rel_path = str(folder)

    logger.info("Diagnostics saved: %s", rel_path)
    return rel_path


@contextmanager
def failure_diagnostics(session):
    """Context manager that captures diagnostics on unhandled exceptions."""
    try:
        yield
    except Exception as exc:
        try:
            capture_failure(session, exc)
        except Exception as cap_exc:
            logger.debug("Diagnostic capture itself failed: %s", cap_exc)
        raise
