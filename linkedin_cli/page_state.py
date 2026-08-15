# linkedin_cli/page_state.py
from enum import Enum

class PageState(str, Enum):
    FEED = "FEED"
    LOGIN = "LOGIN"
    AUTHWALL = "AUTHWALL"
    CHECKPOINT = "CHECKPOINT"
    PROFILE = "PROFILE"
    MESSAGING = "MESSAGING"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"

class PageFlow:
    pass

def classify_page(page) -> PageState:
    url = getattr(page, "url", "") or ""
    path = url.split("?")[0].lower()
    if "/feed" in path:
        return PageState.FEED
    if "/login" in path:
        return PageState.LOGIN
    if "/authwall" in path:
        return PageState.AUTHWALL
    if "/checkpoint" in path:
        return PageState.CHECKPOINT
    if "/in/" in path:
        return PageState.PROFILE
    if "/messaging" in path:
        return PageState.MESSAGING
    if "/404" in path:
        return PageState.NOT_FOUND
    return PageState.UNKNOWN

def transition(page, target: PageState):
    pass
