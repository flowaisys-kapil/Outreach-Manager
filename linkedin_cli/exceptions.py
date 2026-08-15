from outreach_manager.linkedin.browser.exceptions import (
    AuthenticationError,
    BrowserRecoveryFailed,
    CheckpointChallengeError,
    ProfileInaccessibleError,
    ReachedConnectionLimit,
    SkipProfile,
)

class IllegalPageTransition(Exception):
    """Raised when an invalid page transition is attempted."""
    pass

class ActionFailed(Exception):
    """Raised when a browser action fails."""
    pass

__all__ = [
    "AuthenticationError",
    "BrowserRecoveryFailed",
    "CheckpointChallengeError",
    "ProfileInaccessibleError",
    "ReachedConnectionLimit",
    "SkipProfile",
    "IllegalPageTransition",
    "ActionFailed",
]
