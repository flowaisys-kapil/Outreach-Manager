import logging
import sys

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the Outreach Manager daemon (onboard, validate, start task queue)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--exit-on-empty",
            action="store_true",
            help="Exit when the task queue is empty or no tasks are ready immediately.",
        )
        parser.add_argument(
            "--campaign-id",
            type=int,
            help="Run the daemon for a specific campaign ID.",
        )

    def handle(self, *args, **options):
        self._configure_logging(verbose=options["verbosity"] >= 2)
        self._ensure_db()
        self._ensure_onboarded()
        self._nudge_email_setup()
        session = self._create_session(campaign_id=options.get("campaign_id"))

        # Run intelligent profile deduplication sweep
        from outreach_manager.crm.models.lead import Lead
        Lead.perform_deduplication()

        from outreach_manager.core.daemon import run_daemon
        run_daemon(session, exit_on_empty=options["exit_on_empty"])

    # -- Steps ---------------------------------------------------------------

    def _configure_logging(self, verbose: bool = False):
        from outreach_manager.core.logging import configure_logging, print_banner

        level = logging.DEBUG if verbose else logging.INFO
        configure_logging(level=level)
        print_banner()

    def _ensure_db(self):
        call_command("migrate", "--no-input")

        from outreach_manager.core.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        from outreach_manager.core.onboarding import apply, collect_from_wizard, missing_keys

        if not missing_keys():
            return

        if sys.stdin.isatty():
            apply(collect_from_wizard())
        else:
            missing = missing_keys()
            self.stderr.write(
                f"Onboarding incomplete and no TTY available.\n"
                f"Missing: {', '.join(sorted(missing))}\n"
                f"Run with an interactive terminal to complete onboarding."
            )
            sys.exit(1)

    def _nudge_email_setup(self):
        """Prompt (TTY) or log (headless) the next email-setup step. Deferrable —
        never blocks the LinkedIn discovery leg."""
        pass

    def _create_session(self, campaign_id=None):
        from outreach_manager.linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from outreach_manager.core.models import Campaign, SiteConfig

        if not SiteConfig.load().llm_api_key:
            logger.error("LLM_API_KEY is required. Set it in Site Configuration (Django Admin).")
            sys.exit(1)

        profile = get_first_active_profile()
        if profile is None:
            logger.error("No active LinkedIn profiles found.")
            sys.exit(1)

        session = get_or_create_session(profile)

        if campaign_id:
            campaign = Campaign.objects.filter(pk=campaign_id).first()
            if not campaign:
                logger.error("Campaign with ID %s not found.", campaign_id)
                sys.exit(1)
            # Override session.campaigns to isolate run to this specific campaign
            session.__dict__["campaigns"] = [campaign]
        else:
            if not session.campaigns:
                logger.error("No campaigns found for this user.")
                sys.exit(1)
            campaign = session.campaigns[0]

        session.campaign = campaign
        return session


