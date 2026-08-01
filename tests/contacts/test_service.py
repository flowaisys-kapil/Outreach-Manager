# tests/contacts/test_service.py
"""Tests asserting that the Central Contacts Store integration is completely disabled."""
from unittest.mock import patch
import pytest

from outreach_manager.contacts import service
from tests.factories import LeadFactory


@pytest.mark.django_db
def test_resolve_always_returns_none():
    lead = LeadFactory(public_identifier="jane-doe")
    with patch("requests.get") as mock_get:
        assert service.resolve(lead) is None
    mock_get.assert_not_called()


@pytest.mark.django_db
def test_contribute_is_always_noop():
    lead = LeadFactory(public_identifier="jane-doe", country_code="us")
    session = None  # Not even needed since it returns immediately
    with patch("requests.post") as mock_post:
        service.contribute(session, lead, ["jane@acme.com"], "bettercontact")
    mock_post.assert_not_called()
