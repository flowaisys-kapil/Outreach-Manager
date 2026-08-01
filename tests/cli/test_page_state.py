"""Tests for the page-state classifier, the @transition contract, and PageFlow.

No browser: a fake session exposes a ``page.url`` we can set, so the classifier
and the observe→act driver are exercised purely. The whole point of the module
is that page state is judged from the live URL path, so that's all the fake needs.
"""
import pytest

from linkedin_cli.exceptions import IllegalPageTransition
from linkedin_cli.page_state import PageFlow, PageState, classify_page, transition


class _FakePage:
    def __init__(self, url):
        self.url = url


class _FakeSession:
    def __init__(self, url):
        self.page = _FakePage(url)


# ── classify_page ──────────────────────────────────────────────────

@pytest.mark.parametrize("url, expected", [
    ("https://www.linkedin.com/feed/", PageState.FEED),
    ("https://www.linkedin.com/login/", PageState.LOGIN),
    ("https://www.linkedin.com/authwall?trk=x", PageState.AUTHWALL),
    ("https://www.linkedin.com/checkpoint/challenge/", PageState.CHECKPOINT),
    ("https://www.linkedin.com/in/alice-smith/", PageState.PROFILE),
    ("https://www.linkedin.com/messaging/thread/2-abc/", PageState.MESSAGING),
    ("https://www.linkedin.com/404/", PageState.NOT_FOUND),
    ("about:blank", PageState.UNKNOWN),
])
def test_classify_page_by_path(url, expected):
    assert classify_page(_FakePage(url)) is expected


def test_classify_ignores_redirect_query_param():
    """The regression: a /login URL carrying the feed in ?session_redirect= must
    classify as LOGIN, not FEED — the query string is not part of the judgement."""
    url = ("https://www.linkedin.com/login/"
           "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F")
    assert classify_page(_FakePage(url)) is PageState.LOGIN


# ── @transition contract ───────────────────────────────────────────

def test_transition_rejects_wrong_precondition():
    @transition(when=PageState.LOGIN, then={PageState.FEED})
    def act(session):
        pass

    session = _FakeSession("https://www.linkedin.com/feed/")  # not LOGIN
    with pytest.raises(IllegalPageTransition, match="requires page state 'login'"):
        act(session)


def test_transition_rejects_illegal_postcondition():
    @transition(when=PageState.LOGIN, then={PageState.FEED})
    def act(session):
        pass  # page stays on /login → rejected-creds shape

    session = _FakeSession("https://www.linkedin.com/login/")
    with pytest.raises(IllegalPageTransition, match="produced 'login'"):
        act(session)


def test_transition_returns_observed_state_and_exposes_contract():
    @transition(when=PageState.LOGIN, then={PageState.FEED, PageState.CHECKPOINT})
    def act(session):
        session.page.url = "https://www.linkedin.com/feed/"

    session = _FakeSession("https://www.linkedin.com/login/")
    assert act(session) is PageState.FEED
    assert act.when is PageState.LOGIN
    assert act.then == frozenset({PageState.FEED, PageState.CHECKPOINT})


# ── PageFlow driver ────────────────────────────────────────────────

def _login_then_feed_flow():
    flow = PageFlow("test", goal=PageState.FEED)

    @flow.transition(when=PageState.UNKNOWN, then={PageState.LOGIN})
    def _from_unknown(session):
        session.page.url = "https://www.linkedin.com/login/"

    @flow.transition(when=PageState.LOGIN, then={PageState.FEED})
    def _from_login(session):
        session.page.url = "https://www.linkedin.com/feed/"

    return flow


def test_flow_drives_to_goal():
    flow = _login_then_feed_flow()
    session = _FakeSession("about:blank")  # UNKNOWN → LOGIN → FEED
    assert flow.run(session) is PageState.FEED


def test_flow_raises_when_no_action_for_state():
    flow = _login_then_feed_flow()
    session = _FakeSession("https://www.linkedin.com/checkpoint/x")
    with pytest.raises(IllegalPageTransition, match="no transition from 'checkpoint'"):
        flow.run(session)


def test_flow_raises_when_goal_not_reached_in_hops():
    flow = PageFlow("loop", goal=PageState.FEED)

    @flow.transition(when=PageState.UNKNOWN, then={PageState.UNKNOWN})
    def _spin(session):
        pass  # never progresses

    with pytest.raises(IllegalPageTransition, match="did not reach 'feed'"):
        flow.run(_FakeSession("about:blank"), max_hops=3)


def test_flow_rejects_duplicate_transition_for_a_state():
    flow = PageFlow("dup", goal=PageState.FEED)

    @flow.transition(when=PageState.LOGIN, then={PageState.FEED})
    def _a(session):
        pass

    with pytest.raises(ValueError, match="already has a transition from 'login'"):
        @flow.transition(when=PageState.LOGIN, then={PageState.FEED})
        def _b(session):
            pass
