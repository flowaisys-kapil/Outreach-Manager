# tests/conftest.py
from unittest.mock import patch

import numpy as np
import pytest

from outreach_manager.core.management.setup_crm import setup_crm
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _ensure_crm_data(db):
    """
    Ensure CRM bootstrap data exists before every test.
    Uses `db` fixture (not transactional_db) for compatibility.
    Since transaction=True tests rollback, we re-create data each time.
    """
    setup_crm()


@pytest.fixture(autouse=True)
def _mock_embeddings(request):
    """Stub fastembed so tests don't need the ONNX model."""
    if "no_embed_mock" in request.keywords:
        yield
    else:
        with patch("outreach_manager.linkedin.ml.embeddings.embed_text", return_value=np.ones(384)):
            yield


@pytest.fixture(autouse=True)
def _mock_contact_capture(request):
    """Stub the LinkedIn contact-info scrape so CONNECTED transitions don't hit a
    live browser. Opt out with the `no_contact_capture_mock` marker — the dedicated
    capture tests mock the lower linkedin_cli boundary to exercise the real method."""
    if "no_contact_capture_mock" in request.keywords:
        yield
    else:
        with patch("outreach_manager.crm.models.lead.Lead.capture_contact_info", return_value=None):
            yield


class FakeAccountSession:
    """Minimal stand-in for AccountSession — exposes django_user + campaign."""

    def __init__(self, django_user, linkedin_profile, campaign):
        self.django_user = django_user
        self.linkedin_profile = linkedin_profile
        self.campaign = campaign
        self.self_profile = {
            "first_name": "Diego",
            "last_name": "Ramirez",
            "urn": "urn:li:fsd_profile:TEST",
        }
        # Resolved post-login on the real session; None here → no active-hours
        # gating (planner tests disable active hours regardless).
        self.active_timezone = None

    def wait(self, min_delay=0.1, max_delay=0.2):
        pass

    @property
    def campaigns(self):
        from outreach_manager.core.models import Campaign
        return Campaign.objects.filter(users=self.django_user)

    def ensure_browser(self):
        pass

    def is_browser_healthy(self) -> bool:
        return True

    def reset_recovery_state(self):
        """No-op — test sessions never have real browser state."""
        pass


@pytest.fixture
def fake_session(db):
    """An AccountSession-like object backed by the Django test DB."""
    from outreach_manager.core.models import Campaign
    from outreach_manager.linkedin.models import LinkedInProfile

    user = UserFactory(username="testuser")

    campaign = Campaign.objects.first()
    if campaign is None:
        campaign = Campaign.objects.create(name="LinkedIn Outreach")
    campaign.users.add(user)

    linkedin_profile, _ = LinkedInProfile.objects.get_or_create(
        user=user,
        defaults={
            "linkedin_username": "testuser@example.com",
            "linkedin_password": "testpass",
        },
    )

    return FakeAccountSession(django_user=user, linkedin_profile=linkedin_profile, campaign=campaign)
