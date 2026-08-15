# openoutreach/core/execution.py
"""Execution Mode Router and Entry Points for OpenOutreach.

Routes application startup based on the centralized runtime configuration:
  - Manual Mode: Executes a single outreach session and exits (v1.0 behavior).
  - Automatic Mode: Initializes the automatic scheduled execution path.
"""
from __future__ import annotations

import logging
from typing import Any

from outreach_manager.core.config import get_config
from outreach_manager.core.routine_planner import calculate_next_execution_time
from outreach_manager.core.session_executor import run_session, SessionSummary
from outreach_manager.core.windows_scheduler import remove_windows_scheduled_task, update_windows_scheduled_task

logger = logging.getLogger(__name__)


def run_manual_mode(session: Any, exit_on_empty: bool = False) -> SessionSummary:
    """Execute a single outreach session in MANUAL mode and exit (v1.0 behavior)."""
    logger.info("[MODE] Operating in MANUAL mode — running single outreach session.")
    remove_windows_scheduled_task()
    return run_session(session, exit_on_empty=exit_on_empty)


def run_automatic_mode(session: Any, exit_on_empty: bool = False) -> SessionSummary:
    """Execute outreach in AUTOMATIC mode using self-scheduling architecture.

    Flow:
      1. Calculate single next execution time.
      2. Update Windows Task Scheduler (overwrite existing trigger).
      3. Execute ONE outreach session.
      4. Exit cleanly.
    """
    logger.info("[MODE] Operating in AUTOMATIC mode — calculating single next execution time.")
    next_run = calculate_next_execution_time()
    logger.info("[SCHEDULE] Single Next Execution Time: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    # Overwrite Windows Task Scheduler trigger before session execution
    update_windows_scheduled_task(next_run)

    # Execute one session and exit cleanly
    return run_session(session, exit_on_empty=exit_on_empty)


def start_execution(session: Any, exit_on_empty: bool = False) -> SessionSummary:
    """Main application entry point. Route startup based on config.runtime.execution_mode."""
    config = get_config()
    mode = config.runtime.execution_mode.lower()

    if mode == "automatic":
        return run_automatic_mode(session, exit_on_empty=exit_on_empty)
    else:
        return run_manual_mode(session, exit_on_empty=exit_on_empty)
