# outreach_manager/linkedin/tasks/extract_leads.py
"""Batch workflow handler: pure read-only LinkedIn search extraction."""
import logging
from termcolor import colored

from outreach_manager.linkedin.pipeline import pools as _pools
from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)


def handle_extract_leads(task, session, qualifiers) -> bool:
    """Searches LinkedIn for new leads via campaign keywords, qualifies all in batch, and saves to DB.

    Isolation Boundary:
    - ONLY navigates to LinkedIn search result pages.
    - ONLY reads profile data, runs LLM qualification, and stores results.
    - NEVER sends connection requests or messages during this step.
    """
    campaign = session.campaign
    logger.info(
        "[%s] %s — Starting search extraction batch...",
        campaign,
        colored("▶ extract_leads", "magenta", attrs=["bold"])
    )

    qualifier = qualifiers.get(campaign.pk) if qualifiers else None
    if qualifier is None:
        logger.warning("[%s] extract_leads: no qualifier loaded — slot skipped", campaign)
        return WorkflowResult()

    extracted_count = 0
    errors_count = 0
    errors_list: list[str] = []

    try:
        qualify_gen = _pools.qualify_source(session, qualifier)
        for extracted_pid in qualify_gen:
            if extracted_pid:
                extracted_count += 1
                logger.info(
                    "[%s] extract_leads SUCCESS: Extracted & qualified new lead '%s'",
                    campaign, extracted_pid
                )
    except Exception as e:
        from outreach_manager.core.llm import is_quota_error
        if is_quota_error(e):
            provider = getattr(e, "provider", "LLM Provider")
            logger.info(
                "[%s] LLM quota exhausted (Provider: %s) during lead extraction — batch deferred to next session.",
                campaign, provider,
            )
        else:
            logger.warning("[%s] extract_leads error during batch extraction: %s", campaign, e)
            errors_count += 1
            errors_list.append(f"extract_leads error: {e}")

    logger.info(
        "[%s] Extract Leads Workflow — Extracted & Qualified: %d lead(s), Errors: %d",
        campaign, extracted_count, errors_count
    )

    from outreach_manager.core.workflow_result import WorkflowResult
    return WorkflowResult(
        processed_count=extracted_count,
        skipped_count=0,
        error_count=errors_count,
        errors=errors_list,
    )
