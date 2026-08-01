# outreach_manager/core/apps.py
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "outreach_manager.core"
    label = "core"
    default_auto_field = "django.db.models.BigAutoField"
