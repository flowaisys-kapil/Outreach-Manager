# outreach_manager/core/pacer.py
"""Workflow pacing delay helper component."""
from __future__ import annotations

import logging
import random
import time
from outreach_manager.core.models import Task
from outreach_manager.core.sequence_generator import (
    PACING_AFTER_WORK_MIN,
    PACING_AFTER_WORK_MAX,
    PACING_AFTER_SKIP_MIN,
    PACING_AFTER_SKIP_MAX,
)

logger = logging.getLogger(__name__)


class WorkflowPacer:
    """Executes pacing delays between workflow steps."""

    @staticmethod
    def pace_after_step(task_type: Task.TaskType, action_performed: bool) -> None:
        """Enforce standard jitter delays after executing or skipping a workflow."""
        if action_performed:
            sleep_secs = random.uniform(PACING_AFTER_WORK_MIN, PACING_AFTER_WORK_MAX)
            logger.info("Pacing %ds after %s (action performed)", int(sleep_secs), task_type.value)
        else:
            sleep_secs = random.uniform(PACING_AFTER_SKIP_MIN, PACING_AFTER_SKIP_MAX)
            logger.debug("Skip pause %ds after %s (no conditions met)", int(sleep_secs), task_type.value)
        time.sleep(sleep_secs)
