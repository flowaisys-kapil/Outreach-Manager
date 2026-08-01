from django.contrib import admin
from outreach_manager.crm.models.lead import Lead
from outreach_manager.crm.models.deal import Deal

@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("public_identifier", "linkedin_url", "disqualified", "creation_date")
    search_fields = ("public_identifier", "linkedin_url")
    list_filter = ("disqualified", "creation_date")

@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("lead", "campaign", "state", "outcome")
    search_fields = ("lead__public_identifier", "campaign__campaign_objective", "reason")
    list_filter = ("state", "outcome", "campaign")
