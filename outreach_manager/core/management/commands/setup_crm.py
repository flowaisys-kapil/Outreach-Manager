from django.core.management.base import BaseCommand

from outreach_manager.core.management.setup_crm import setup_crm


class Command(BaseCommand):
    help = "Bootstrap CRM data (default site). Idempotent."

    def handle(self, *args, **options):
        setup_crm()
