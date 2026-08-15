# outreach_manager/linkedin/browser/exceptions.py
"""Browser lifecycle exceptions.

Kept in a separate module so that any code importing these exceptions
does not trigger the heavyweight browser layer imports.
"""
from __future__ import annotations


class BrowserRecoveryFailed(Exception):
    """Raised by AccountSession.ensure_browser() when browser recovery
    was attempted but start_browser_session() raised an exception.
    """
    pass


class AuthenticationError(Exception):
    """Raised when authentication fails or session expires."""
    pass


class CheckpointChallengeError(Exception):
    """Raised when LinkedIn presents a security checkpoint/challenge."""
    pass


class ProfileInaccessibleError(Exception):
    """Raised when a profile cannot be accessed."""
    pass


class ReachedConnectionLimit(Exception):
    """Raised when connection invite limit is reached."""
    pass


class SkipProfile(Exception):
    """Raised when a profile should be skipped."""
    pass
