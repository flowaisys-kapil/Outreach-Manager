# outreach_manager/core/workflow_runner.py
"""Workflow runner for executing specific tasks with error diagnostics and exception mapping."""
from __future__ import annotations

import logging
from typing import Callable
from termcolor import colored

from outreach_manager.core.models import Task
from outreach_manager.core.workflow_result import WorkflowResult
from outreach_manager.core.sequence_generator import WorkflowLock
from outreach_manager.linkedin.browser.exceptions import (
    AuthenticationError,
    BrowserRecoveryFailed,
    CheckpointChallengeError,
)
from outreach_manager.linkedin.diagnostics import failure_diagnostics
from outreach_manager.emails.tasks.send import handle_email
from outreach_manager.linkedin.tasks.check_pending import handle_check_pending
from outreach_manager.linkedin.tasks.connect import handle_connect
from outreach_manager.linkedin.tasks.follow_up import handle_follow_up
from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from outreach_manager.linkedin.tasks.first_message import handle_first_message

# Register EXTRACT_LEADS if available
try:
    from outreach_manager.linkedin.tasks.extract_leads import handle_extract_leads
except ImportError:
    handle_extract_leads = None

logger = logging.getLogger(__name__)

_WORKFLOW_HANDLERS: dict[Any, Callable] = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.CHECK_PENDING: handle_check_pending,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.EMAIL: handle_email,
    Task.TaskType.REPLY_UNREAD: handle_reply_unread,
    Task.TaskType.FIRST_MESSAGE: handle_first_message,
    "connect": handle_connect,
    "check_pending": handle_check_pending,
    "follow_up": handle_follow_up,
    "email": handle_email,
    "reply_unread": handle_reply_unread,
    "first_message": handle_first_message,
    "CONNECT": handle_connect,
    "CHECK_PENDING": handle_check_pending,
    "FOLLOW_UP": handle_follow_up,
    "EMAIL": handle_email,
    "REPLY_UNREAD": handle_reply_unread,
    "FIRST_MESSAGE": handle_first_message,
}
if handle_extract_leads is not None:
    _WORKFLOW_HANDLERS[Task.TaskType.EXTRACT_LEADS] = handle_extract_leads
    _WORKFLOW_HANDLERS["extract_leads"] = handle_extract_leads
    _WORKFLOW_HANDLERS["EXTRACT_LEADS"] = handle_extract_leads



def initialize_qualifiers(session, qualifiers: dict | None) -> dict:
    """Initialize and warm-start Bayesian qualifiers for each campaign."""
    if qualifiers is None:
        qualifiers = {}
    from outreach_manager.core.conf import CAMPAIGN_CONFIG
    from outreach_manager.linkedin.ml.qualifier import BayesianQualifier
    from outreach_manager.crm.models import Lead

    for campaign in session.campaigns:
        if campaign.pk not in qualifiers:
            q = BayesianQualifier(
                seed=42,
                n_mc_samples=CAMPAIGN_CONFIG["qualification_n_mc_samples"],
                campaign=campaign,
            )
            X, y = Lead.get_labeled_arrays(campaign)
            if len(X) > 0:
                q.warm_start(X, y)
            qualifiers[campaign.pk] = q
    return qualifiers


class WorkflowRunner:
    """Encapsulates the execution logic for a single workflow type.

    Does not contain policy checks, pacing sleep delays, or reverse lookups.
    """

    def __init__(self, session, task_type: Task.TaskType, handler: Callable, qualifiers: dict | None = None):
        self.session = session
        self.task_type = task_type
        self.handler = handler
        self.qualifiers = qualifiers or {}

    def execute(self, summary) -> bool:
        """Execute the workflow across campaigns.

        Returns True if at least one real action was performed, False otherwise.
        """
        wf_name = self.task_type.value

        # Reset recovery state so each workflow gets a fresh recovery chance.
        if hasattr(self.session, 'reset_recovery_state'):
            self.session.reset_recovery_state()

        action_performed = False
        try:
            for campaign in self.session.campaigns:
                self.session.campaign = campaign
                with WorkflowLock.acquire(self.task_type):
                    with failure_diagnostics(self.session):
                        result = self.handler(None, self.session, self.qualifiers)
                        if isinstance(result, WorkflowResult):
                            if result.processed_count > 0:
                                action_performed = True
                                summary.actions_performed += result.processed_count
                            if result.error_count > 0:
                                summary.deal_errors += result.error_count
                                summary.errors.extend(result.errors)
                            if getattr(result, "llm_deferrals_count", 0) > 0:
                                summary.llm_deferrals += result.llm_deferrals_count
                            metrics = getattr(result, "metrics", None) or {}
                            for k, v in metrics.items():
                                summary.aggregated_metrics[k] = summary.aggregated_metrics.get(k, 0) + v
                        elif result is True or (hasattr(result, "__bool__") and bool(result) is True):
                            action_performed = True
                            summary.actions_performed += 1

            if action_performed:
                summary.workflows_executed.append(wf_name)
            else:
                summary.workflows_skipped.append(f"{wf_name} (No Work Available)")
                summary.workflows_no_work.append(wf_name)

        except BrowserRecoveryFailed as exc:
            logger.warning(
                "[WARN] Browser recovery failed.\n  Workflow: %s\n  Recovery Attempts: 1\n  Workflow skipped.",
                self.task_type.value,
            )
            summary.workflow_errors += 1
            summary.errors.append(f"{self.task_type.value}: browser recovery failed")
            summary.workflows_skipped.append(self.task_type.value)

        except CheckpointChallengeError as exc:
            raise

        except AuthenticationError:
            logger.warning("Session expired during %s — re-authenticating", self.task_type.value)
            try:
                self.session.reauthenticate()
            except CheckpointChallengeError as exc:
                raise
            except Exception as reauth_err:
                logger.exception("Re-authentication failed: %s", reauth_err)
                summary.fatal_errors += 1
                summary.errors.append(f"Reauth failure: {reauth_err}")

        except Exception as exc:
            logger.exception("Workflow %s encountered error: %s", self.task_type.value, exc)
            summary.workflow_errors += 1
            summary.errors.append(f"{self.task_type.value}: {exc}")
            summary.workflows_skipped.append(self.task_type.value)

        return action_performed
