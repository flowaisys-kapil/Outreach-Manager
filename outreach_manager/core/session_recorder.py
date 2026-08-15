# openoutreach/core/session_recorder.py
"""Neutral Flight Recorder for OpenOutreach.

Records completed session history, AI usage statistics, and provider health metrics
without modifying execution architecture, scheduling, or browser automation.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from django.utils import timezone

from outreach_manager.core.config import get_config
from outreach_manager.core.models import AIUsageLog, ProviderHealth, SessionHistory

if TYPE_CHECKING:
    from outreach_manager.core.session_executor import SessionSummary

logger = logging.getLogger(__name__)


class SessionRecorder:
    """Neutral recorder service persisting session history and AI usage telemetry."""

    @classmethod
    def record_session(
        cls,
        summary: "SessionSummary",
        execution_mode: str = "manual",
    ) -> SessionHistory | None:
        """Persist session history summary and AI usage metrics if enabled in config."""
        diag_cfg = get_config().diagnostics
        if not diag_cfg.session_history_enabled:
            logger.debug("[SessionRecorder] session_history_enabled=False — skipping session recording.")
            return None

        finish_time = summary.finish_time or timezone.now()
        duration_seconds = summary.duration_seconds
        if duration_seconds <= 0.0 and summary.start_time:
            duration_seconds = (finish_time - summary.start_time).total_seconds()

        # Derive status
        if summary.fatal_errors > 0 or (summary.workflow_errors > 0 and summary.actions_performed == 0):
            status = "Failed"
        elif summary.total_errors > 0:
            status = "Completed with Errors"
        elif summary.actions_performed == 0:
            skipped_str = " ".join(summary.workflows_skipped)
            if "Outside Active Hours" in skipped_str or "outside active hours" in skipped_str.lower():
                status = "Skipped (Outside Active Hours)"
            else:
                status = "Skipped (No Work)"
        else:
            status = "Completed"

        session_id = str(uuid.uuid4())
        history_record = SessionHistory.objects.create(
            session_id=session_id,
            start_time=summary.start_time,
            finish_time=finish_time,
            duration_seconds=round(duration_seconds, 2),
            execution_mode=execution_mode,
            workflows_executed=list(summary.workflows_executed),
            workflows_disabled=list(summary.workflows_disabled),
            workflows_skipped=list(summary.workflows_skipped),
            actions_completed=summary.actions_performed,
            deal_errors=summary.deal_errors,
            workflow_errors=summary.workflow_errors,
            fatal_errors=summary.fatal_errors,
            browser_recoveries=summary.browser_recoveries,
            llm_deferrals=summary.llm_deferrals,
            diagnostics_generated=summary.diagnostics_generated,
            total_errors=summary.total_errors,
            status=status,
        )

        # Record AI Usage Telemetry if enabled
        if diag_cfg.ai_usage_tracking_enabled:
            from outreach_manager.core.llm import get_ai_usage_tracker
            tracker = get_ai_usage_tracker()
            ai_snapshot = tracker.drain()

            ai_log = AIUsageLog.objects.create(
                session=history_record,
                primary_provider=ai_snapshot.get("primary_provider", get_config().ai.primary_provider),
                fallback_provider=ai_snapshot.get("fallback_provider", get_config().ai.fallback_provider),
                primary_calls=ai_snapshot.get("primary_calls", 0),
                fallback_calls=ai_snapshot.get("fallback_calls", 0),
                successful_calls=ai_snapshot.get("successful_calls", 0),
                failed_calls=ai_snapshot.get("failed_calls", 0),
                structured_output_calls=ai_snapshot.get("structured_output_calls", 0),
                retries=ai_snapshot.get("retries", 0),
                estimated_input_tokens=ai_snapshot.get("estimated_input_tokens", 0),
                estimated_output_tokens=ai_snapshot.get("estimated_output_tokens", 0),
            )

            # Update summary AI attributes for logging
            summary.ai_calls = ai_log.primary_calls + ai_log.fallback_calls
            summary.primary_provider = ai_log.primary_provider
            summary.fallback_used = ai_log.fallback_calls > 0
            summary.provider_failures = ai_log.failed_calls

            # Persist ProviderHealth once per session per provider used
            provider_telemetry = ai_snapshot.get("provider_telemetry", {})
            for provider_name, p_stats in provider_telemetry.items():
                try:
                    ProviderHealth.record_batch(
                        provider_name=provider_name,
                        total_calls=p_stats.get("total_calls", 0),
                        successful_calls=p_stats.get("successful_calls", 0),
                        failure_count=p_stats.get("failure_count", 0),
                        fallback_invocations=p_stats.get("fallback_invocations", 0),
                        response_times_ms=p_stats.get("response_times_ms", []),
                    )
                except Exception as ph_exc:
                    logger.debug("[SessionRecorder] Failed to persist ProviderHealth for %s: %s", provider_name, ph_exc)

        logger.info(
            "[SessionRecorder] Saved session history record (ID: %s, Status: %s, Actions: %d).",
            session_id[:8], status, summary.actions_performed,
        )
        return history_record
