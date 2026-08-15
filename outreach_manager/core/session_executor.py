# outreach_manager/core/session_executor.py
"""Session Executor — Single-session outreach lifecycle engine.

Orchestrates the sequence of events in an outreach session:
  1. Startup: Active hours & due work gates, scheduler reconciliation.
  2. Randomization: Generate a single randomized weighted workflow sequence.
  3. Execution: Run each workflow in the sequence exactly once.
  4. Summary: Log session summary, release resources, and exit cleanly.
"""
from __future__ import annotations

import logging
import sys
from termcolor import colored

from django.utils import timezone

from outreach_manager.core.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ENABLE_ACTIVE_HOURS,
)
from outreach_manager.linkedin.scheduler import reconcile, earliest_due_time, has_due_work
from outreach_manager.core.sequence_generator import get_execution_sequence, BalancedSequenceGenerator
from outreach_manager.core.models import Task
from outreach_manager.linkedin.browser.exceptions import CheckpointChallengeError

from .session_summary import SessionSummary
from .workflow_runner import WorkflowRunner, initialize_qualifiers, _WORKFLOW_HANDLERS
from .workflow_policy import WorkflowExecutionPolicy
from .pacer import WorkflowPacer
from outreach_manager.core.workflow_result import WorkflowResult
from outreach_manager.core.sequence_generator import WorkflowLock

logger = logging.getLogger(__name__)

__all__ = [
    "SessionSummary",
    "run_session",
    "seconds_until_active",
    "_WORKFLOW_HANDLERS",
    "WorkflowResult",
    "WorkflowLock",
    "BalancedSequenceGenerator",
    "reconcile",
    "has_due_work",
]


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


def run_session(session, qualifiers: dict | None = None, exit_on_empty: bool = False) -> SessionSummary:
    """Execute a single outreach session.

    Performs startup checks, generates a single weighted workflow sequence,
    executes each workflow in order, logs a session summary, and exits cleanly.
    """
    summary = SessionSummary(
        start_time=timezone.now(),
        color_output=True,
        execution_mode="manual",
    )

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

    # Active-hours gate check
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

    # Guarantee healthy browser session before executing sequence
    if hasattr(session, "ensure_browser") and callable(session.ensure_browser):
        try:
            session.ensure_browser()
        except Exception as exc:
            logger.error("[Session Executor] Browser initialization failed: %s. Aborting session.", exc)
            summary.errors.append(f"Browser initialization failed: {exc}")
            summary.workflow_errors += 1
            summary.finish_time = timezone.now()
            summary.duration_seconds = (summary.finish_time - summary.start_time).total_seconds()
            summary.log_summary()
            return summary

    if hasattr(session, "is_browser_healthy") and callable(session.is_browser_healthy):
        if not session.is_browser_healthy():
            err_msg = "Browser session is unhealthy or closed. Aborting outreach cycle."
            logger.error("[Session Executor] %s", err_msg)
            summary.errors.append(err_msg)
            summary.workflow_errors += 1
            summary.finish_time = timezone.now()
            summary.duration_seconds = (summary.finish_time - summary.start_time).total_seconds()
            summary.log_summary()
            return summary


    # Prepare Bayesian Qualifiers if needed
    qualifiers = initialize_qualifiers(session, qualifiers)



    # 4. Generate workflow execution sequence (Sequence planning decoupled)
    cycle_sequence = get_execution_sequence(session)

    formatted_order = " -> ".join([
        colored(t.value.upper(), "yellow", attrs=["bold"]) for t in cycle_sequence
    ])
    banner = colored("==================== [ WORKFLOW EXECUTION SEQUENCE ] ====================", "cyan", attrs=["bold"])
    logger.info("\n%s\n  Decided Order: %s\n%s\n", banner, formatted_order, banner)

    # 5. Execute each workflow in the sequence exactly once
    try:
        total_steps = len(cycle_sequence)
        for idx, task_type in enumerate(cycle_sequence, start=1):
            wf_name = task_type.value
            step_header = colored(f"[Step {idx}/{total_steps}]", "magenta", attrs=["bold"])
            task_label = colored(wf_name.upper(), "yellow", attrs=["bold"])

            # 5a. Policy check
            eligible, reason = WorkflowExecutionPolicy.check_eligibility(session, task_type)
            if not eligible:
                if reason == "Disabled":
                    summary.workflows_skipped.append(f"{wf_name} (Disabled)")
                    summary.workflows_disabled.append(wf_name)
                elif reason == "Daily Limit Reached":
                    summary.workflows_skipped.append(f"{wf_name} (Daily Limit Reached)")
                    summary.workflows_limit_reached.append(wf_name)
                continue

            logger.info("%s Executing workflow: %s", step_header, task_label)

            # 5b. Resolve workflow handler using the registry inside executor scope
            handler = _WORKFLOW_HANDLERS.get(task_type)
            if handler is None:
                logger.error("No handler registered for workflow: %s", task_type)
                summary.workflows_skipped.append(wf_name)
                continue

            # 5c. Invoke WorkflowRunner (pure execution)
            runner = WorkflowRunner(session, task_type, handler, qualifiers)
            action_performed = False
            try:
                action_performed = runner.execute(summary)
            except CheckpointChallengeError as exc:
                logger.error(
                    colored(
                        f"ACCOUNT CHECKPOINTED during {task_type.value} — "
                        f"{getattr(getattr(session, 'linkedin_profile', None), 'linkedin_username', 'unknown')}",
                        "red", attrs=["bold"],
                    )
                )
                summary.fatal_errors += 1
                summary.errors.append(f"Checkpoint Challenge: {exc}")
                if hasattr(session, "close"):
                    session.close()
                sys.exit(1)

            # 5d. Execute pacing delays
            if idx < total_steps:
                WorkflowPacer.pace_after_step(task_type, action_performed)

        summary.finish_time = timezone.now()
        summary.duration_seconds = (summary.finish_time - summary.start_time).total_seconds()
        
        br_val = getattr(session, "browser_recoveries", None)
        if isinstance(br_val, int):
            summary.browser_recoveries = br_val
            
        dg_val = getattr(session, "diagnostics_generated", None)
        if isinstance(dg_val, int):
            summary.diagnostics_generated = dg_val

        # Print/Log Summary
        summary.log_summary()
        return summary
    finally:
        # Cleanup browser resources
        if hasattr(session, "close"):
            try:
                session.close()
            except Exception as exc:
                logger.debug("[SessionExecutor] Error in final browser cleanup: %s", exc)
