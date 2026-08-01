import sys

from django.core.management.base import BaseCommand

from outreach_manager.core.conf import (
    DEFAULT_CONNECT_DAILY_LIMIT,
    DEFAULT_FOLLOW_UP_DAILY_LIMIT,
)


class Command(BaseCommand):
    help = "Run onboarding (interactive or non-interactive with CLI flags or --config-file)."

    def add_arguments(self, parser):
        parser.add_argument("--non-interactive", action="store_true")
        parser.add_argument(
            "--config-file",
            help="JSON file with onboard config (avoids shell-escaping issues).",
        )
        # Individual flags (used when --config-file is not provided)
        parser.add_argument("--linkedin-email", default="")
        parser.add_argument("--linkedin-password", default="")
        parser.add_argument("--campaign-name", default="")
        parser.add_argument("--product-description", default="")
        parser.add_argument("--campaign-objective", default="")
        parser.add_argument("--booking-link", default="")
        parser.add_argument("--seed-urls", default="")
        parser.add_argument("--llm-api-key", default="")
        parser.add_argument("--ai-model", default="")
        parser.add_argument("--llm-api-base", default="")
        parser.add_argument("--connect-daily-limit", type=int, default=DEFAULT_CONNECT_DAILY_LIMIT)
        parser.add_argument("--follow-up-daily-limit", type=int, default=DEFAULT_FOLLOW_UP_DAILY_LIMIT)
        parser.add_argument("--legal-acceptance", action="store_true")

    def handle(self, *args, **options):
        from outreach_manager.core.onboarding import (
            OnboardConfig, apply, collect_from_wizard, missing_keys,
        )

        if not options["non_interactive"]:
            if not sys.stdin.isatty():
                self.stderr.write(
                    "No TTY available. Use --non-interactive with --config-file or flags."
                )
                sys.exit(1)
            if not missing_keys():
                return
            config = collect_from_wizard()
            apply(config)
            return

        if options["config_file"]:
            config = OnboardConfig.from_json(options["config_file"])
        else:
            config = OnboardConfig(
                linkedin_email=options["linkedin_email"],
                linkedin_password=options["linkedin_password"],
                campaign_name=options["campaign_name"],
                product_description=options["product_description"],
                campaign_objective=options["campaign_objective"],
                booking_link=options["booking_link"],
                seed_urls=options["seed_urls"],
                llm_api_key=options["llm_api_key"],
                ai_model=options["ai_model"],
                llm_api_base=options["llm_api_base"],
                connect_daily_limit=options["connect_daily_limit"],
                follow_up_daily_limit=options["follow_up_daily_limit"],
                legal_acceptance=options["legal_acceptance"],
            )

        if not config.linkedin_email:
            self.stderr.write("linkedin_email is required in non-interactive mode")
            sys.exit(1)
        if not config.linkedin_password:
            self.stderr.write("linkedin_password is required in non-interactive mode")
            sys.exit(1)

        apply(config)
