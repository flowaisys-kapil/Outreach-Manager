"""Management command: rebuild chat summaries for selected Deals.

Usage examples:

    python manage.py rebuild_chat_summaries
    python manage.py rebuild_chat_summaries --campaign-id 3
    python manage.py rebuild_chat_summaries --deal-id 42
    python manage.py rebuild_chat_summaries --campaign-id 3 --dry-run

Purpose:
    Bulk repair of contaminated ``deal.chat_summary`` fields.  Rebuilds each
    matched Deal's summary from scratch using only its own ``ChatMessage``
    rows and the corrected Phase 4 extraction/reconciliation prompts (which
    include lead identity grounding).

    This command is deliberately NOT triggered automatically — running it is
    an explicit repair action.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Rebuild deal.chat_summary from ChatMessage rows using Phase 4 prompts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--deal-id",
            type=int,
            dest="deal_id",
            default=None,
            help="Rebuild only this specific Deal (by PK).",
        )
        parser.add_argument(
            "--campaign-id",
            type=int,
            dest="campaign_id",
            default=None,
            help="Rebuild all Deals in this campaign.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Log what would be rebuilt without saving anything.",
        )

    def handle(self, *args, **options):
        from outreach_manager.crm.models import Deal
        from outreach_manager.core.db.summaries import (
            _build_lead_identity_context,
            rebuild_chat_summary,
        )

        dry_run: bool = options["dry_run"]
        deal_id: int | None = options["deal_id"]
        campaign_id: int | None = options["campaign_id"]

        qs = Deal.objects.select_related("lead", "campaign")
        if deal_id is not None:
            qs = qs.filter(pk=deal_id)
        if campaign_id is not None:
            qs = qs.filter(campaign_id=campaign_id)

        deals = list(qs)
        if not deals:
            self.stdout.write(self.style.WARNING("No deals matched the given filters."))
            return

        if dry_run:
            self.stdout.write(
                self.style.NOTICE(f"[dry-run] Would rebuild {len(deals)} deal(s):")
            )
            for deal in deals:
                from outreach_manager.chat.models import ChatMessage
                msg_count = ChatMessage.objects.filter(deal=deal).count()
                self.stdout.write(
                    f"  deal={deal.pk} lead={deal.lead.public_identifier} "
                    f"campaign={deal.campaign_id} messages={msg_count}"
                )
            return

        # Need a seller name — use the campaign's linked user if available,
        # falling back to a generic placeholder so the command works without
        # an active session.
        rebuilt = 0
        failed = 0
        for deal in deals:
            seller_name = _resolve_seller_name(deal)
            lead_identity = _build_lead_identity_context(deal)
            try:
                rebuild_chat_summary(deal, seller_name=seller_name, lead_identity=lead_identity)
                rebuilt += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [OK] deal={deal.pk} lead={deal.lead.public_identifier}"
                    )
                )
            except Exception as exc:
                failed += 1
                logger.exception(
                    "rebuild_chat_summaries: failed for deal=%s: %s", deal.pk, exc
                )
                self.stdout.write(
                    self.style.ERROR(
                        f"  [FAIL] deal={deal.pk} lead={deal.lead.public_identifier}: {exc}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Rebuilt: {rebuilt}, Failed: {failed}, Total: {len(deals)}"
            )
        )


def _resolve_seller_name(deal) -> str:
    """Best-effort seller name for a deal without an active session."""
    # Try campaign owner
    campaign = deal.campaign
    owner = getattr(campaign, "user", None) or getattr(campaign, "owner", None)
    if owner:
        first = getattr(owner, "first_name", "") or ""
        if first.strip():
            return first.strip()
        return owner.username or "Me"
    # Fall back to first superuser
    User = get_user_model()
    su = User.objects.filter(is_superuser=True).first()
    if su:
        return (su.first_name or su.username or "Me").strip()
    return "Me"
