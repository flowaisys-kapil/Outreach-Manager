from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Delete all Leads, Deals, "
        "ActionLogs, reset SearchKeywords, and clear GP model blobs. "
        "Keeps Campaigns and LinkedInProfiles."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip confirmation prompt.",
        )

    def handle(self, *args, **options):
        from outreach_manager.crm.models import Deal, Lead

        from outreach_manager.core.models import Campaign
        from outreach_manager.linkedin.models import ActionLog, SearchKeyword

        counts = {
            "Leads": Lead.objects.count(),
            "Deals": Deal.objects.count(),
            "ActionLogs": ActionLog.objects.count(),
            "SearchKeywords (to reset)": SearchKeyword.objects.count(),
        }

        self.stdout.write("Will delete:")
        for name, count in counts.items():
            self.stdout.write(f"  {name}: {count}")

        if not options["yes"]:
            confirm = input("\nProceed? [y/N] ")
            if confirm.lower() != "y":
                self.stdout.write("Aborted.")
                return

        # Order matters: delete dependents first
        Deal.objects.all().delete()
        ActionLog.objects.all().delete()
        Lead.objects.all().delete()

        # Reset search keywords to unused
        SearchKeyword.objects.update(used=False, used_at=None)

        self.stdout.write(self.style.SUCCESS("Reset complete."))
