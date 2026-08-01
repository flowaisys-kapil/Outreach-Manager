"""Tests for the linkedin_cli session registry (name → bound-browser endpoint)."""
from linkedin_cli import session as session_mod


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLI_HOME", str(tmp_path))
    session_mod.write_session("work", "ws://127.0.0.1:5000/tok", pid=4242)

    record = session_mod.read_session("work")
    assert record == {"name": "work", "endpoint": "ws://127.0.0.1:5000/tok", "pid": 4242}


def test_read_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLI_HOME", str(tmp_path))
    assert session_mod.read_session("absent") is None


def test_clear_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLI_HOME", str(tmp_path))
    session_mod.write_session("work", "ws://x/tok", pid=1)
    session_mod.clear_session("work")
    assert session_mod.read_session("work") is None


def test_clear_missing_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLI_HOME", str(tmp_path))
    session_mod.clear_session("never-existed")  # must not raise
