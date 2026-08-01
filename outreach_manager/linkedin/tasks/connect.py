# outreach_manager/linkedin/tasks/connect.py
"""Connect task — resolves candidates from the campaign pool and acts in a batch."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from termcolor import colored

from outreach_manager.core.db.deals import increment_connect_attempts, set_profile_state
from outreach_manager.crm.models import DealState, Deal
from outreach_manager.linkedin.db.leads import disqualify_lead
from outreach_manager.linkedin.models import ActionLog
from linkedin_cli.exceptions import ProfileInaccessibleError, ReachedConnectionLimit, SkipProfile

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3


@dataclass
class ConnectStrategy:
    find_candidate: Callable
    pre_connect: Callable | None
    qualifier: object


def strategy_for(campaign, qualifiers):
    """Build the right ConnectStrategy based on campaign type."""
    qualifier = qualifiers.get(campaign.pk)

    from outreach_manager.linkedin.pipeline.pools import find_candidate
    from outreach_manager.core.models import SiteConfig
    from django.utils import timezone

    site_config = SiteConfig.load()

    override_active = False
    if site_config.simulated_task and site_config.override_expires_at:
        if timezone.now() < site_config.override_expires_at:
            override_active = True

    active_mode = site_config.simulated_task if override_active else ""
    if not active_mode:
        now_local = timezone.now().astimezone()
        hour = now_local.hour
        if hour >= 20 or hour < 8:
            active_mode = "nighttime"

    if active_mode in ["extract", "nighttime"]:
        backfill = True
    else:
        backfill = False

    return ConnectStrategy(
        find_candidate=lambda s: find_candidate(s, qualifier, backfill=backfill),
        pre_connect=None,
        qualifier=qualifier,
    )


def handle_connect(task, session, qualifiers) -> bool:
    from linkedin_cli.actions.connect import send_connection_request
    from linkedin_cli.actions.status import get_connection_status

    campaign = session.campaign
    strategy = strategy_for(campaign, qualifiers)

    from outreach_manager.core.models import SiteConfig
    from django.utils import timezone

    site_config = SiteConfig.load()
    override_active = bool(site_config.simulated_task and site_config.override_expires_at and timezone.now() < site_config.override_expires_at)
    active_mode = site_config.simulated_task if override_active else ""

    # Extract mode only scrapes profiles — batch iterate qualify_gen
    if active_mode == "extract":
        logger.info("[%s] %s — Running live LinkedIn Search lead extraction & qualification batch...", campaign, colored("▶ extract", "magenta", attrs=["bold"]))
        from outreach_manager.linkedin.pipeline.pools import qualify_source
        qualify_gen = qualify_source(session, strategy.qualifier)
        extracted_count = 0
        for extracted_pid in qualify_gen:
            if extracted_pid:
                extracted_count += 1
                logger.info("[%s] Search Extraction SUCCESS: Extracted and qualified new lead %s", campaign, extracted_pid)
        logger.info("[%s] Extract Mode Batch Complete — %d lead(s) extracted", campaign, extracted_count)
        return extracted_count > 0

    processed_count = 0
    skipped_count = 0
    errors_count = 0
    errors_list: list[str] = []

    while session.linkedin_profile.can_execute(ActionLog.ActionType.CONNECT):
        candidate = strategy.find_candidate(session)
        if candidate is None:
            break

        public_id = candidate["public_identifier"]
        profile = candidate.get("profile") or candidate

        deal = Deal.objects.filter(
            lead__public_identifier=public_id,
            campaign=session.campaign,
        ).first()

        if deal and deal.state in (DealState.CONNECTED, DealState.PENDING, DealState.FAILED):
            logger.info("[%s] connect: %s is already %s — skipping profile visit", campaign, public_id, deal.state)
            skipped_count += 1
            continue

        reason = deal.reason if deal else ""
        stats = strategy.qualifier.explain(candidate, session) if strategy.qualifier else ""
        logger.info("[%s] %s", campaign, colored("▶ connect", "cyan", attrs=["bold"]))
        logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

        try:
            status = DealState(get_connection_status(session, profile).value)

            if status in (DealState.CONNECTED, DealState.PENDING):
                set_profile_state(session, public_id, status.value)
                if deal:
                    from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                    next_t = schedule_next_action(deal)
                    log_execution(deal, "CONNECT", f"Observed Status {status.value}", deal.state, next_t)
                processed_count += 1
                continue

            new_state = DealState(send_connection_request(session=session, profile=profile).value)

            if new_state == DealState.QUALIFIED:
                attempts = increment_connect_attempts(session, public_id)
                if attempts >= MAX_CONNECT_ATTEMPTS:
                    reason = f"Unreachable: no Connect button after {attempts} attempts"
                    disqualify_lead(public_id)
                    set_profile_state(session, public_id, DealState.FAILED.value, reason=reason)
                    logger.warning("Disqualified %s — %s", public_id, reason)
                    if deal:
                        from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                        next_t = schedule_next_action(deal)
                        log_execution(deal, "CONNECT", "Disqualified (No Connect Button)", "NONE", None)
                    skipped_count += 1
                else:
                    set_profile_state(session, public_id, new_state.value)
                    logger.debug("%s: connect attempt %d/%d — no button found", public_id, attempts, MAX_CONNECT_ATTEMPTS)
                    if deal:
                        from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                        next_t = schedule_next_action(deal)
                        log_execution(deal, "CONNECT", f"Attempt {attempts}/{MAX_CONNECT_ATTEMPTS} No Button", "CONNECT", next_t)
                    processed_count += 1
            else:
                from django.utils import timezone as _tz
                Deal.objects.filter(
                    lead__public_identifier=public_id, campaign=session.campaign,
                ).update(connection_requested_at=_tz.now())
                set_profile_state(session, public_id, new_state.value)
                session.linkedin_profile.record_action(
                    ActionLog.ActionType.CONNECT, session.campaign,
                )
                if deal:
                    deal.refresh_from_db()
                    from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                    next_t = schedule_next_action(deal)
                    log_execution(deal, "CONNECT", "Connection Request Sent", "CHECK_PENDING", next_t)

                try:
                    from outreach_manager.crm.models.event_log import EventLog
                    EventLog.objects.create(
                        campaign=session.campaign,
                        deal=deal,
                        event_type=EventLog.EventType.CONNECT_REQUESTED,
                        detail=f"Connection request sent to {public_id}"
                    )
                except Exception as e:
                    logger.warning("Failed to log connect_requested event: %s", e)

                if hasattr(session, "connects_sent_this_run"):
                    session.connects_sent_this_run += 1
                processed_count += 1

        except ReachedConnectionLimit as e:
            logger.warning("Rate limited: %s", e)
            session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
            processed_count += 1
            break

        except ProfileInaccessibleError as e:
            logger.warning("Profile inaccessible — marking FAILED: %s", e)
            set_profile_state(session, public_id, DealState.FAILED.value,
                              reason=f"Profile inaccessible: {e}")
            if deal:
                from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                schedule_next_action(deal)
                log_execution(deal, "CONNECT", f"Profile Inaccessible: {e}", "NONE", None)
            skipped_count += 1

        except SkipProfile as e:
            logger.warning("Skipping %s: %s", public_id, e)
            set_profile_state(session, public_id, DealState.FAILED.value)
            if deal:
                from outreach_manager.linkedin.scheduler import log_execution, schedule_next_action
                schedule_next_action(deal)
                log_execution(deal, "CONNECT", f"Skipped Profile: {e}", "NONE", None)
            skipped_count += 1

        except Exception as exc:
            logger.exception("connect batch error for %s: %s", public_id, exc)
            errors_count += 1
            errors_list.append(f"connect error for {public_id}: {exc}")

    logger.info(
        "[%s] Connect Workflow — Processed: %d, Skipped: %d, Errors: %d",
        campaign, processed_count, skipped_count, errors_count
    )

    from outreach_manager.core.workflow_result import WorkflowResult
    return WorkflowResult(
        processed_count=processed_count,
        skipped_count=skipped_count,
        error_count=errors_count,
        errors=errors_list,
    )
