"""Tests for the linkedin_cli verb CLI — dispatch, error mapping, handle parsing.

The session is mocked at the boundary (no real browser): we inject a fake
session via ``read_session`` + ``PlaywrightCliSession`` so the verb runner is
exercised without Playwright.
"""
import json

import pytest

from linkedin_cli import cli
from linkedin_cli.exceptions import AuthenticationError, ProfileInaccessibleError


# ── _handle_to_profile ─────────────────────────────────────────────

def test_handle_to_profile_from_id():
    out = cli._handle_to_profile("alice-smith")
    assert out["public_identifier"] == "alice-smith"
    assert out["url"] == "https://www.linkedin.com/in/alice-smith/"


def test_handle_to_profile_from_url():
    out = cli._handle_to_profile("https://www.linkedin.com/in/alice-smith/")
    assert out["public_identifier"] == "alice-smith"


def test_handle_to_profile_rejects_non_profile_url():
    with pytest.raises(ValueError):
        cli._handle_to_profile("https://www.linkedin.com/feed/")


# ── error-type mapping ─────────────────────────────────────────────

def test_error_type_maps_known_exceptions():
    assert cli._error_type(AuthenticationError("x")) == "authentication"
    assert cli._error_type(ProfileInaccessibleError("x")) == "profile_inaccessible"


def test_error_type_unknown_returns_none():
    assert cli._error_type(ValueError("x")) is None


# ── verb runner dispatch ───────────────────────────────────────────

class _FakeSession:
    """Minimal stand-in injected in place of PlaywrightCliSession."""

    def __init__(self, *args, **kwargs):
        self.closed = False
        self._raise = None

    def ensure_browser(self):
        pass

    @property
    def self_profile(self):
        if self._raise:
            raise self._raise
        return {"public_identifier": "me-self", "urn": "urn:li:fsd_profile:ME", "full_name": "Me Self"}

    def close(self):
        self.closed = True


@pytest.fixture
def injected_session(monkeypatch):
    """Route the verb runner to a fake session and a present registry entry."""
    session = _FakeSession()
    monkeypatch.setattr(cli, "read_session", lambda name: {"endpoint": "ws://x/abc", "pid": 1})
    monkeypatch.setattr(cli, "PlaywrightCliSession", lambda *a, **k: session)
    return session


def test_whoami_human_default(injected_session, capsys):
    """Default output is the brief human summary, not JSON."""
    code = cli.main(["whoami", "--session", "work"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Me Self (me-self)"
    assert injected_session.closed  # session always released


def test_whoami_json_emits_full_dict(injected_session, capsys):
    """--json emits the verb's full result dict on stdout."""
    code = cli.main(["whoami", "--json", "--session", "work"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert code == 0
    assert payload == {"self": {"public_identifier": "me-self", "urn": "urn:li:fsd_profile:ME", "full_name": "Me Self"}}


def test_known_error_goes_to_stderr(injected_session, capsys):
    injected_session._raise = AuthenticationError("session expired")
    code = cli.main(["whoami", "--session", "work"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.strip() == ""                      # stdout carries only results
    assert "error: authentication: session expired" in captured.err
    assert injected_session.closed


def test_unknown_error_propagates(injected_session):
    injected_session._raise = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        cli.main(["whoami", "--session", "work"])
    assert injected_session.closed  # released even when the error propagates


def test_missing_session_is_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_session", lambda name: None)
    code = cli.main(["profile", "alice", "--session", "nope"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no open session named 'nope'" in captured.err


# ── human renderers (pure dict → brief summary) ────────────────────

def test_human_state_is_the_bare_word():
    assert cli._human_state({"public_identifier": "alice", "state": "Connected"}) == "Connected"


def test_human_sent():
    assert cli._human_sent({"sent": True}) == "sent"
    assert cli._human_sent({"sent": False}) == "not sent"


def test_human_identity():
    assert cli._human_identity({"self": {"public_identifier": "alice-smith", "full_name": "Alice Smith"}}) \
        == "Alice Smith (alice-smith)"


def test_human_profile_is_a_brief_summary():
    summary = cli._human_profile({
        "full_name": "Alice Smith", "headline": "Engineer",
        "location_name": "Berlin", "industry": {"name": "Software"},
        "positions": [1, 2, 3], "educations": [1],
    })
    assert summary.splitlines() == [
        "Alice Smith — Engineer",
        "Berlin · Software",
        "3 positions · 1 schools",
        "(--json for the full record)",
    ]


def test_human_thread_empty_vs_messages():
    assert cli._human_thread({"messages": None}) == "(no conversation)"
    rendered = cli._human_thread({"messages": [
        {"timestamp": "2026-05-01 14:03", "sender": "Alice", "text": "hi"}]})
    assert rendered == "2026-05-01 14:03  Alice: hi"


def test_render_json_vs_human(capsys):
    cli._render("status", {"public_identifier": "alice", "state": "Pending"}, as_json=False)
    assert capsys.readouterr().out.strip() == "Pending"
    cli._render("status", {"public_identifier": "alice", "state": "Pending"}, as_json=True)
    assert json.loads(capsys.readouterr().out.strip()) == {"public_identifier": "alice", "state": "Pending"}


def test_human_search_lists_handles():
    rendered = cli._human_search({"page": 1, "profiles": [
        {"public_identifier": "alice", "url": "u"}, {"public_identifier": "bob", "url": "u"}]})
    assert rendered.splitlines() == ["2 result(s) on page 1:", "  alice", "  bob"]
    assert cli._human_search({"profiles": []}) == "(no results)"


def test_verb_search_maps_network_words_to_codes(injected_session, monkeypatch):
    captured = {}

    def fake_search_people(session, keywords, page=1, network=None):
        captured.update(keywords=keywords, page=page, network=network)
        return {"query": keywords, "page": page, "network": network, "profiles": []}

    monkeypatch.setattr("linkedin_cli.actions.search.search_people", fake_search_people)
    code = cli.main(["search", "head of growth", "--network", "first", "--page", "2", "--session", "work"])
    assert code == 0
    assert captured == {"keywords": "head of growth", "page": 2, "network": ["F"]}


# ── search URL building (actions.search._search_url) ───────────────

def test_search_url_keyword_only():
    from linkedin_cli.actions.search import _search_url
    url = _search_url("San Francisco")
    assert url.startswith("https://www.linkedin.com/search/results/people/?")
    assert "keywords=San+Francisco" in url
    assert "network" not in url and "page" not in url


def test_search_url_with_network_and_page():
    from linkedin_cli.actions.search import _search_url
    url = _search_url("ceo", page=3, network=["F", "S"])
    assert "network=%5B%22F%22%2C+%22S%22%5D" in url  # JSON ["F", "S"], url-encoded
    assert "page=3" in url
