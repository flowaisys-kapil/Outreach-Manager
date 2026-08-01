# outreach_manager/emails/apps.py
from django.apps import AppConfig


class EmailsConfig(AppConfig):
    name = "outreach_manager.emails"
    label = "emails"
    default_auto_field = "django.db.models.BigAutoField"
