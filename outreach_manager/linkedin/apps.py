# outreach_manager/linkedin/apps.py
from django.apps import AppConfig


class LinkedInConfig(AppConfig):
    name = "outreach_manager.linkedin"
    label = "linkedin"
    default_auto_field = "django.db.models.BigAutoField"
