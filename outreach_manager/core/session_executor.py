# outreach_manager/core/session_executor.py
"""Session Executor — Single-session outreach lifecycle engine.

Replaces the legacy permanent execution daemon with a clean, single-session executor.
One launch equals one outreach session:
  1. Startup: Active hours & due work gates, scheduler reconciliation.
  2. Randomization: Generate a single randomized weighted workflow sequence.
  3. Execution: Run each workflow in the sequence exactly once.
  4. Summary: Log session summary, release resources, and exit cleanly.
"""
from __future__ import annotations

import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from django.utils import timezone
from termcolor import colored

from outreach_manager.core.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    CAMPAIGN_CONFIG,
    ENABLE_ACTIVE_HOURS,
)
from linkedin_cli.exceptions import AuthenticationError, CheckpointChallengeError
from outreach_manager.linkedin.browser.exceptions import BrowserRecoveryFailed
from outreach_manager.linkedin.diagnostics import failure_diagnostics
from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
from outreach_manager.core.models import Task, SiteConfig
from outreach_manager.emails.tasks.send import handle_email
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending
from outreach_manager.linkedin.tasks.connect import handle_connect
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from outreach_manager.linkedin.tasks.first_message import handle_first_message
from outreach_manager.linkedin.tasks.extract_leads import handle_extract_leads
from outreach_manager.linkedin.scheduler import reconcile, earliest_due_time, has_due_work
from outreach_manager.core.sequence_generator import (
    BalancedSequenceGenerator,
    WorkflowLock,
    PACING_AFTER_WORK_MIN,
    PACING_AFTER_WORK_MAX,
    PACING_AFTER_SKIP_MIN,
    PACING_AFTER_SKIP_MAX,
)
from outreach_manager.core.workflow_result import WorkflowResult

logger = logging.getLogger(__name__)

_WORKFLOW_HANDLERS: dict[Task.TaskType, Callable] = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.CHECK_PENDING: handle_check_pending,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.EMAIL: handle_email,
    Task.TaskType.REPLY_UNREAD: handle_reply_unread,
    Task.TaskType.FIRST_MESSAGE: handle_first_message,
    Task.TaskType.EXTRACT_LEADS: handle_extract_leads,
}



@dataclass
class SessionSummary:
    """Authoritative structured summary of an outreach session."""
    start_time: datetime
    finish_time: datetime | None = None
    duration_seconds: float = 0.0
    workflows_executed: list[str] = field(default_factory=list)
    workflows_skipped: list[str] = field(default_factory=list)
    actions_performed: int = 0
    deal_errors: int = 0
    workflow_errors: int = 0
    fatal_errors: int = 0
    browser_recoveries: int = 0      # successful browser recoveries during this session
    llm_deferrals: int = 0           # clean LLM quota deferrals during this session
    diagnostics_generated: int = 0    # count of diagnostic packages saved
    errors: list[str] = field(default_factory=list)

    @property
    def total_errors(self) -> int:
        return self.deal_errors + self.workflow_errors + self.fatal_errors

    def log_summary(self) -> None:
        """Output structured session summary to log."""
        dur_str = f"{int(self.duration_seconds // 60)}m {int(self.duration_seconds % 60)}s"
        banner = colored("==================== [ OUTREACH SESSION SUMMARY ] ====================", "cyan", attrs=["bold"])
        lines = [
            f"  Start Time:            {self.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"  Finish Time:           {self.finish_time.strftime('%Y-%m-%d %H:%M:%S UTC') if self.finish_time else 'N/A'}",
            f"  Duration:              {dur_str}",
            f"  Workflows Executed:    {len(self.workflows_executed)} ({', '.join(self.workflows_executed) if self.workflows_executed else 'None'})",
            f"  Workflows Skipped:     {len(self.workflows_skipped)} ({', '.join(self.workflows_skipped) if self.workflows_skipped else 'None'})",
            f"  Actions Completed:     {self.actions_performed}",
            f"  Deal Errors:           {self.deal_errors}",
            f"  Workflow Errors:       {self.workflow_errors}",
            f"  Browser Recoveries:    {self.browser_recoveries}",
            f"  LLM Deferrals:         {self.llm_deferrals}",
            f"  Fatal Errors:          {self.fatal_errors}",
            f"  Diagnostics Generated: {self.diagnostics_generated}",
            f"  Total Errors:          {self.total_errors}",
        ]
        if self.errors:
            lines.append("  Error Details:")
            for err in self.errors:
                lines.append(f"    - {err}")
        logger.info("\n%s\n%s\n%s\n", banner, "\n".join(lines), banner)


def seconds_until_active(tz_name: str | None = None) -> float:
    """Seconds until the active-hours window opens."""
    if not ENABLE_ACTIVE_HOURS or tz_name is None:
        return 0.0
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now_local = timezone.now().astimezone(tz)
    cur_h = now_local.hour
    if ACTIVE_START_HOUR <= cur_h < ACTIVE_END_HOUR:
        return 0.0
    if cur_h >= ACTIVE_END_HOUR:
        tomorrow_start = (now_local + timezone.timedelta(days=1)).replace(
            hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0,
        )
        return (tomorrow_start - now_local).total_seconds()
    today_start = now_local.replace(
        hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0,
    )
    return (today_start - now_local).total_seconds()


def _is_network_error(exc: Exception) -> bool:
    name = type(exc).__name__
    msg = str(exc).lower()
    return name in ("PlaywrightError", "Error") and any(
        kw in msg for kw in (
            "net::err_", "timeout", "target closed", "browser has been closed",
            "navigation failed", "connection refused", "reset by peer",
        )
    )


def run_session(session, qualifiers: dict | None = None, exit_on_empty: bool = False) -> SessionSummary:
    """Execute a single outreach session.

    Performs startup checks, generates a single weighted workflow sequence,
    executes each workflow in order, logs a session summary, and exits cleanly.
    """
    summary = SessionSummary(start_time=timezone.now())
    campaigns = session.campaigns

    logger.info(
        colored("[Session Executor] Starting outreach session", "cyan", attrs=["bold"])
        + " — %d campaign(s)", len(campaigns),
    )

    if ENABLE_ACTIVE_HOURS:
        logger.info(
            "Active hours %02d:00–%02d:00 — timezone %s",
            ACTIVE_START_HOUR, ACTIVE_END_HOUR, session.active_timezone_provenance(),
        )
    else:
        logger.info("Active hours disabled — running session")

    # 1. Startup Reconcile
    reconcile(session)

    # 2. Active-hours gate check
    pause = seconds_until_active(session.active_timezone)
    if pause > 0:
        logger.info(
            "Outside active hours (%02d:00–%02d:00 %s) — session skipped.",
            ACTIVE_START_HOUR, ACTIVE_END_HOUR, session.active_timezone,
        )
        summary.finish_time = timezone.now()
        summary.duration_seconds = (summary.finish_time - summary.start_time).total_seconds()
        summary.log_summary()
        return summary

    # 3. Due-work gate check
    if not has_due_work(session.campaigns):
        earliest = earliest_due_time(session.campaigns)
        earliest_str = earliest.strftime("%Y-%m-%d %H:%M UTC") if earliest else "None scheduled"
        logger.info(
            colored("[Session Executor] No work due right now", "cyan")
            + " (next action at %s) — exiting session without browser launch.",
            earliest_str,
        )
        summary.finish_time = timezone.now()
        summary.duration_seconds = (summary.finish_time - summary.start_time).total_seconds()
        summary.log_summary()
        return summary

    # Prepare Bayesian Qualifiers if needed
    if qualifiers is None:
        qualifiers = {}
        for campaign in campaigns:
            if campaign.pk not in qualifiers:
                from outreach_manager.crm.models import Lead
                q = BayesianQualifier(
                    seed=42,
                    n_mc_samples=CAMPAIGN_CONFIG["qualification_n_mc_samples"],
                    campaign=campaign,
                )
                X, y = Lead.get_labeled_arrays(campaign)
                if len(X) > 0:
                    q.warm_start(X, y)
                qualifiers[campaign.pk] = q

    # 4. Generate probabilistic workflow sequence (or apply task override)
    site_config = SiteConfig.load()
    override_task = site_config.simulated_task
    if override_task and site_config.override_expires_at and timezone.now() > site_config.override_expires_at:
        site_config.simulated_task = ""
        site_config.override_expires_at = None
        site_config.save()
        override_task = ""

    task_override_map = {
        "reply_unread": Task.TaskType.REPLY_UNREAD,
        "follow_up": Task.TaskType.FOLLOW_UP,
        "first_message": Task.TaskType.FIRST_MESSAGE,
        "check_pending": Task.TaskType.CHECK_PENDING,
        "connect": Task.TaskType.CONNECT,
        "extract_leads": Task.TaskType.EXTRACT_LEADS,
        "extract": Task.TaskType.EXTRACT_LEADS,
    }

    if override_task and override_task in task_override_map:
        override_type = task_override_map[override_task]
        cycle_sequence = [override_type]
        banner = colored(f"==================== [ SESSION (TASK OVERRIDE: {override_type.value.upper()}) ] ====================", "yellow", attrs=["bold"])
        logger.info("\n%s\n  Executing ONLY: %s\n%s\n", banner, colored(override_type.value.upper(), "yellow", attrs=["bold"]), banner)
    else:
        cycle_sequence = BalancedSequenceGenerator.get_cycle_sequence(session)
        formatted_order = " -> ".join([
            colored(t.value.upper(), "yellow", attrs=["bold"]) for t in cycle_sequence
        ])
        banner = colored("==================== [ WORKFLOW EXECUTION SEQUENCE ] ====================", "cyan", attrs=["bold"])
        logger.info("\n%s\n  Decided Order: %s\n%s\n", banner, formatted_order, banner)

    # 5. Execute each workflow in the sequence exactly once
    total_steps = len(cycle_sequence)
    for idx, task_type in enumerate(cycle_sequence, start=1):
        step_header = colored(f"[Step {idx}/{total_steps}]", "magenta", attrs=["bold"])
        task_label = colored(task_type.value.upper(), "yellow", attrs=["bold"])
        logger.info("%s Executing workflow: %s", step_header, task_label)

        handler = _WORKFLOW_HANDLERS.get(task_type)
        if handler is None:
            logger.error("No handler registered for workflow: %s", task_type)
            summary.workflows_skipped.append(task_type.value)
            continue

        # Reset recovery state so each workflow gets a fresh recovery chance.
        # A browser failure in workflow N does not permanently block workflow N+1.
        if hasattr(session, 'reset_recovery_state'):
            session.reset_recovery_state()

        action_performed = False
        try:
            for campaign in campaigns:
                session.campaign = campaign
                with WorkflowLock.acquire(task_type):
                    with failure_diagnostics(session):
                        result = handler(None, session, qualifiers)
                        if isinstance(result, WorkflowResult):
                            if result.processed_count > 0:
                                action_performed = True
                                summary.actions_performed += result.processed_count
                            if result.error_count > 0:
                                summary.deal_errors += result.error_count
                                summary.errors.extend(result.errors)
                            if getattr(result, "llm_deferrals_count", 0) > 0:
                                summary.llm_deferrals += result.llm_deferrals_count
                        elif result is True or (hasattr(result, "__bool__") and bool(result) is True):
                            action_performed = True
                            summary.actions_performed += 1

            if action_performed:
                summary.workflows_executed.append(task_type.value)
            else:
                summary.workflows_skipped.append(task_type.value)

        except BrowserRecoveryFailed as exc:
            logger.warning(
                "[WARN] Browser recovery failed.\n  Workflow: %s\n  Recovery Attempts: 1\n  Workflow skipped.",
                task_type.value,
            )
            summary.workflow_errors += 1
            summary.errors.append(f"{task_type.value}: browser recovery failed")
            summary.workflows_skipped.append(task_type.value)

        except CheckpointChallengeError as exc:
            logger.error(
                colored(
                    f"ACCOUNT CHECKPOINTED during {task_type.value} — "
                    f"{session.linkedin_profile.linkedin_username}",
                    "red", attrs=["bold"],
                )
            )
            summary.fatal_errors += 1
            summary.errors.append(f"Checkpoint Challenge: {exc}")
            session.close()
            sys.exit(1)

        except AuthenticationError:
            logger.warning("Session expired during %s — re-authenticating", task_type.value)
            try:
                session.reauthenticate()
            except CheckpointChallengeError as exc:
                logger.error("ACCOUNT CHECKPOINTED during reauth: %s", exc.url)
                summary.fatal_errors += 1
                summary.errors.append(f"Checkpoint Challenge: {exc}")
                session.close()
                sys.exit(1)
            except Exception as reauth_err:
                logger.exception("Re-authentication failed: %s", reauth_err)
                summary.fatal_errors += 1
                summary.errors.append(f"Reauth failure: {reauth_err}")

        except Exception as exc:
            logger.exception("Workflow %s encountered error: %s", task_type.value, exc)
            summary.workflow_errors += 1
            summary.errors.append(f"{task_type.value}: {exc}")
            summary.workflows_skipped.append(task_type.value)

        # Pacing between workflow steps
        if idx < total_steps:
            if action_performed:
                sleep_secs = random.uniform(PACING_AFTER_WORK_MIN, PACING_AFTER_WORK_MAX)
                logger.info("Pacing %ds after %s (action performed)", int(sleep_secs), task_type.value)
            else:
                sleep_secs = random.uniform(PACING_AFTER_SKIP_MIN, PACING_AFTER_SKIP_MAX)
                logger.debug("Skip pause %ds after %s (no conditions met)", int(sleep_secs), task_type.value)
            time.sleep(sleep_secs)

    summary.finish_time = timezone.now()
    summary.duration_seconds = (summary.finish_time - summary.start_time).total_seconds()
    summary.browser_recoveries = getattr(session, "browser_recoveries", summary.browser_recoveries)
    summary.diagnostics_generated = getattr(session, "diagnostics_generated", summary.diagnostics_generated)
    summary.log_summary()

    return summary
