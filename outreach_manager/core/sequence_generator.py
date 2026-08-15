# outreach_manager/core/sequence_generator.py
"""Decoupled Probabilistic Workflow Engine — sequence generator and orchestration primitives.

The engine drives the outreach cycle via a randomized weighted sequence of
all 6 workflows, executed once per cycle. Each workflow evaluates its own
conditions at execution time and returns True if real work was performed,
False if conditions were not met (skipped).

Pacing rules (from architecture spec):
  - After real work (action_performed=True):  45–90s jitter pause
  - After skip (action_performed=False):       3–8s short pause
  - Between full cycles:                       _HumanRhythmBreak from daemon
"""
import logging
import random
import threading
from typing import List

from django.utils import timezone

from outreach_manager.core.models import Task

logger = logging.getLogger(__name__)

# Balanced base selection weights matching the architecture spec exactly.
# Weights: REPLY_UNREAD=20, FOLLOW_UP=20, FIRST_MESSAGE=20,
#          CHECK_ACCEPTANCES(CHECK_PENDING)=15, SEND_CONNECT(CONNECT)=15, EXTRACT_LEADS=10
BASE_WEIGHTS = {
    Task.TaskType.REPLY_UNREAD: 20,
    Task.TaskType.FOLLOW_UP: 20,
    Task.TaskType.FIRST_MESSAGE: 20,
    Task.TaskType.CHECK_PENDING: 15,
    Task.TaskType.CONNECT: 15,
    Task.TaskType.EXTRACT_LEADS: 10,
}

# Pacing constants (architecture spec §4)
PACING_AFTER_WORK_MIN = 45.0   # seconds after real action performed
PACING_AFTER_WORK_MAX = 90.0
PACING_AFTER_SKIP_MIN = 3.0    # seconds after skip (no conditions met)
PACING_AFTER_SKIP_MAX = 8.0


class SyntheticTask:
    """Lightweight stand-in for a Task DB row used by cycle-driven workflows.

    The 5 cycle-driven workflows (REPLY_UNREAD, FOLLOW_UP, FIRST_MESSAGE,
    EXTRACT_LEADS, CONNECT) do not require a persisted Task row — they evaluate
    their own eligibility conditions at execution time. This object satisfies
    the `(task, session, qualifiers)` handler signature without a DB write.

    CHECK_PENDING remains Task-row-driven (real Task objects from the DB) so
    per-deal `next_check_pending_at` cooldown timestamps are respected.
    EMAIL is also Task-row-driven (handled separately outside the cycle).
    """

    def __init__(self, task_type: Task.TaskType, campaign_id: int):
        self.task_type = task_type
        self.payload = {"campaign_id": campaign_id}
        self.status = Task.Status.RUNNING
        self._completed = False
        self._failed = False
        # Synthetic tasks have no DB pk
        self.pk = None
        self.scheduled_at = timezone.now()

    def mark_running(self):
        self.status = Task.Status.RUNNING

    def mark_completed(self):
        self.status = Task.Status.COMPLETED
        self._completed = True

    def mark_failed(self):
        self.status = Task.Status.FAILED
        self._failed = True

    def __str__(self):
        return f"SyntheticTask({self.task_type}, campaign={self.payload.get('campaign_id')})"


class WorkflowLock:
    """Thread-level lock guaranteeing only one workflow has browser DOM access at a time.

    Usage (context manager):
        with WorkflowLock.acquire(workflow_name):
            handler(task, session, qualifiers)
    """
    _lock = threading.Lock()
    _current_workflow: str = ""

    @classmethod
    def acquire(cls, workflow_name: str):
        return _WorkflowLockContext(cls._lock, workflow_name)


class _WorkflowLockContext:
    def __init__(self, lock: threading.Lock, name: str):
        self._lock = lock
        self._name = name

    def __enter__(self):
        acquired = self._lock.acquire(timeout=120)
        if not acquired:
            raise RuntimeError(f"WorkflowLock timed out for workflow '{self._name}' after 120s")
        WorkflowLock._current_workflow = self._name
        logger.debug("WorkflowLock ACQUIRED by '%s'", self._name)
        return self

    def __exit__(self, *args):
        WorkflowLock._current_workflow = ""
        self._lock.release()
        logger.debug("WorkflowLock RELEASED by '%s'", self._name)


class BalancedSequenceGenerator:
    """Generates a randomized, weighted sequence of all 6 workflows to run per cycle.

    All 6 workflows are always placed in the sequence (weighted random order).
    Each workflow evaluates its own conditions at execution time and skips if unmet.
    The cycle runs every workflow exactly once, then ends.
    """

    @staticmethod
    def get_cycle_sequence(session) -> List[Task.TaskType]:
        """Returns a weighted random permutation of all 6 workflow task types."""
        items = list(BASE_WEIGHTS.keys())
        weights = [BASE_WEIGHTS[k] for k in items]

        # Weighted random shuffle: pick one by weight, remove, repeat
        shuffled = []
        candidates = list(items)
        cand_weights = list(weights)

        while candidates:
            selected = random.choices(candidates, weights=cand_weights, k=1)[0]
            shuffled.append(selected)
            idx = candidates.index(selected)
            candidates.pop(idx)
            cand_weights.pop(idx)

        logger.info(
            "Cycle Workflow Sequence [%d workflows]: %s",
            len(shuffled),
            [t.value for t in shuffled]
        )
        return shuffled


def get_execution_sequence(session) -> list[Task.TaskType]:
    """Resolve the workflow execution sequence, evaluating task overrides and balanced generator."""
    from outreach_manager.core.models import SiteConfig
    from django.utils import timezone

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
        return [override_type]
    return BalancedSequenceGenerator.get_cycle_sequence(session)

