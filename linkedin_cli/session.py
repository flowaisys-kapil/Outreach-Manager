# linkedin_cli/session.py
"""Session compatibility module for linkedin_cli."""
from outreach_manager.linkedin.browser.registry import (
    get_first_active_profile,
    get_or_create_session,
)

__all__ = [
    "get_first_active_profile",
    "get_or_create_session",
]
