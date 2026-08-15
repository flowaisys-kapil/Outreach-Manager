# outreach_manager/core/workflow_result.py
"""Lightweight data container returned by every workflow handler.

Kept in its own module to avoid circular imports:
  session_executor  imports  workflow tasks
  workflow tasks    import   WorkflowResult

By extracting WorkflowResult here, both sides can import from this neutral
module without triggering a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkflowResult:
    """Lightweight return type for batch workflow execution.

    Every workflow handler must return a WorkflowResult so the Session
    Executor can accurately record deal-level successes and failures.

    The optional ``metrics`` dict allows each workflow to surface
    business-level outcome counts without making WorkflowResult workflow-specific.
    """
    processed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    llm_deferrals_count: int = 0
    errors: list[str] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """True when at least one deal was successfully processed."""
        return self.processed_count > 0
