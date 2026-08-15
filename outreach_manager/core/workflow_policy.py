# outreach_manager/core/workflow_policy.py
"""Policy check component for workflow eligibility (toggles, daily limits, platform limits)."""
from __future__ import annotations

import logging
from outreach_manager.core.models import Task
from outreach_manager.linkedin.models import _RATE_LIMIT_FIELDS

logger = logging.getLogger(__name__)


class WorkflowExecutionPolicy:
    """Evaluates whether a given workflow is eligible to be executed."""

    @staticmethod
    def check_eligibility(session, task_type: Task.TaskType) -> tuple[bool, str]:
        """Check toggle and daily limits for the given workflow.

        Returns (True, "") if eligible, or (False, reason) if ineligible.
        """
        wf_name = task_type.value

        # 1. Daily Limit Check
        profile = getattr(session, "linkedin_profile", None)
        today_count = profile._daily_count(wf_name) if profile and hasattr(profile, "_daily_count") else 0
        user_limit = 9999

        if profile:
            daily_field = _RATE_LIMIT_FIELDS.get(wf_name)
            if daily_field and hasattr(profile, daily_field):
                platform_limit = getattr(profile, daily_field, None)
                if isinstance(platform_limit, int):
                    effective_limit = min(platform_limit, user_limit)
                    if today_count >= effective_limit:
                        logger.info(
                            "[INFO] %s skipped. Daily limit reached (%d/%d).",
                            wf_name.capitalize(),
                            today_count,
                            effective_limit,
                        )
                        return False, "Daily Limit Reached"

        return True, ""
