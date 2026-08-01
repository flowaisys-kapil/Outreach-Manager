# outreach_manager/linkedin/browser/exceptions.py
"""Browser lifecycle exceptions.

Kept in a separate module so that any code importing these exceptions
does not trigger the heavyweight browser layer imports.
"""
from __future__ import annotations


class BrowserRecoveryFailed(Exception):
    """Raised by AccountSession.ensure_browser() when browser recovery
    was attempted but start_browser_session() raised an exception.

    The Session Executor treats this as a workflow-level error:
      - The current workflow is marked failed.
      - The session continues to the next workflow.
      - A browser_recoveries counter is NOT incremented (recovery failed).

    Callers must NOT catch this exception inside per-deal loops —
    let it propagate up to the per-workflow handler in run_session().
    """
