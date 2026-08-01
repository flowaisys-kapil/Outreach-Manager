import pytest
from django.utils import timezone
from outreach_manager.crm.models.lead import Lead
from outreach_manager.crm.models.deal import Deal, DealState
from outreach_manager.chat.models import ChatMessage

@pytest.mark.django_db
class TestIntelligentDeduplication:
    def test_merge_leads_by_api_email_with_deals(self, fake_session):
        campaign = fake_session.campaign

        # Create two leads with same api_email
        lead1 = Lead.objects.create(
            linkedin_url="https://linkedin.com/in/lead-one",
            public_identifier="lead-one",
            api_email="duplicate@email.com"
        )
        lead2 = Lead.objects.create(
            linkedin_url="https://linkedin.com/in/lead-two",
            public_identifier="lead-two",
            api_email="duplicate@email.com"
        )

        # Create deals
        deal1 = Deal.objects.create(lead=lead1, campaign=campaign, state=DealState.QUALIFIED)
        deal2 = Deal.objects.create(lead=lead2, campaign=campaign, state=DealState.CONNECTED)

        # Create chat messages for deal2
        ChatMessage.objects.create(deal=deal2, content="Hello", is_outgoing=False)

        # Perform deduplication
        Lead.perform_deduplication()

        # Check that one lead remains
        assert Lead.objects.count() == 1
        remaining_lead = Lead.objects.first()
        assert remaining_lead.api_email == "duplicate@email.com"

        # Check deal merged and state adopted (since CONNECTED > QUALIFIED)
        assert Deal.objects.count() == 1
        remaining_deal = Deal.objects.first()
        assert remaining_deal.lead == remaining_lead
        assert remaining_deal.state == DealState.CONNECTED

        # Check chat messages migrated
        assert ChatMessage.objects.filter(deal=remaining_deal).count() == 1

    def test_merge_leads_by_api_email(self, fake_session):
        campaign = fake_session.campaign

        # Create two leads with same api_email
        lead1 = Lead.objects.create(
            linkedin_url="https://linkedin.com/in/lead-three",
            public_identifier="lead-three",
            api_email="test@duplicate.com"
        )
        lead2 = Lead.objects.create(
            linkedin_url="https://linkedin.com/in/lead-four",
            public_identifier="lead-four",
            api_email="test@duplicate.com"
        )

        Deal.objects.create(lead=lead1, campaign=campaign, state=DealState.QUALIFIED)
        Deal.objects.create(lead=lead2, campaign=campaign, state=DealState.CONNECTED)

        Lead.perform_deduplication()

        assert Lead.objects.count() == 1
        remaining_lead = Lead.objects.first()
        assert remaining_lead.api_email == "test@duplicate.com"

    def test_merge_leads_by_contact_info_json(self, fake_session):
        campaign = fake_session.campaign

        # Create two leads with same email in contact_info JSON
        lead1 = Lead.objects.create(
            linkedin_url="https://linkedin.com/in/lead-five",
            public_identifier="lead-five",
            contact_info={"email": "json@duplicate.com", "emails": ["json@duplicate.com"]}
        )
        lead2 = Lead.objects.create(
            linkedin_url="https://linkedin.com/in/lead-six",
            public_identifier="lead-six",
            contact_info={"emails": ["json@duplicate.com"]}
        )

        Deal.objects.create(lead=lead1, campaign=campaign, state=DealState.QUALIFIED)
        Deal.objects.create(lead=lead2, campaign=campaign, state=DealState.CONNECTED)

        Lead.perform_deduplication()

        assert Lead.objects.count() == 1
