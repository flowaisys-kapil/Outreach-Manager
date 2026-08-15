# openoutreach/core/agents/reply_agent.py
"""Reply workflow AI agent.

Isolated from the Follow-Up agent so that:
  - Reply and Follow-Up can be patched/mocked independently in tests.
  - Each workflow has a clear, single-responsibility AI entry point.
  - Prompt tuning for one workflow never inadvertently affects the other.

The decision model (ReplyDecision) is structurally identical to FollowUpDecision
because the same set of possible actions applies. It is intentionally re-declared
here so that future prompt or schema divergence requires no structural refactor.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator
from pydantic_ai import Agent

from outreach_manager.core.agents.prompt import _format_facts, base_context, render
from outreach_manager.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)

# Non-empty, non-whitespace string for the message field
_NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

# How many trailing messages the agent sees verbatim alongside the summary
RECENT_MESSAGES_WINDOW = 8  # slightly larger than follow-up: inbound context matters more


class ReplyDecision(BaseModel):
    """Structured output from the reply agent.

    Valid combinations:
      send_message  — message is required (non-empty string)
      mark_completed — outcome is required
      wait          — neither required
    """

    action: Literal["send_message", "mark_completed", "wait"] = Field(
        description=(
            "What to do in response to the inbound message. "
            "'send_message' requires a non-empty message string. "
            "'mark_completed' requires an outcome value. "
            "'wait' requires neither."
        ),
    )
    message: _NonEmptyStr | None = Field(
        default=None,
        description=(
            "The exact reply text to send on LinkedIn. "
            "Required and must be non-empty when action='send_message'. "
            "Omit or set to null for 'wait' or 'mark_completed'."
        ),
    )
    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when action='mark_completed'.",
    )
    follow_up_hours: float = Field(
        description="Hours until next follow-up check. Always required.",
    )

    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError(
                "action='send_message' requires a non-empty 'message' field."
            )
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError(
                "action='mark_completed' requires an 'outcome' value."
            )
        return self


# ── Helpers ───────────────────────────────────────────────────────────────────

def _humanize_age(when: datetime, now: datetime) -> str:
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _format_thread(messages: list, now: datetime) -> str:
    """Render a list of message dicts or ChatMessage ORM objects as a transcript."""
    if not messages:
        return "No messages in thread."
    lines = []
    for m in messages:
        if isinstance(m, dict):
            content = (m.get("text") or "").strip()
            if not content:
                continue
            speaker = "Me" if m.get("is_outgoing") else (m.get("sender") or "Lead")
            lines.append(f"{speaker}: {content}")
        else:
            content = (getattr(m, "content", "") or "").strip()
            if not content:
                continue
            speaker = "Me" if getattr(m, "is_outgoing", False) else "Lead"
            dt = getattr(m, "creation_date", None)
            prefix = f"{speaker} ({_humanize_age(dt, now)})" if dt else speaker
            lines.append(f"{prefix}: {content}")
    return "\n".join(lines) or "No messages in thread."


def _count_new_inbound(messages: list) -> int:
    """Count trailing inbound messages (i.e. messages from lead since last outgoing)."""
    count = 0
    for m in reversed(messages):
        is_out = m.get("is_outgoing") if isinstance(m, dict) else getattr(m, "is_outgoing", False)
        if is_out:
            break
        count += 1
    return count


def _render_system_prompt(session, deal, conversation_history: list) -> str:
    """Build the reply agent system prompt from the reply_agent.j2 template."""
    from django.utils import timezone

    now = timezone.now()
    recent = conversation_history[-RECENT_MESSAGES_WINDOW:]
    chat_facts = _format_facts(deal.chat_summary) if deal else "(none yet)"
    inbound_count = _count_new_inbound(recent)

    return render(
        "reply_agent.j2",
        **base_context(session, deal),
        contact_email=getattr(getattr(session, "linkedin_profile", None), "linkedin_username", ""),
        chat_summary=chat_facts,
        recent_messages=_format_thread(recent, now),
        today=now.strftime("%Y-%m-%d"),
        inbound_message_count=inbound_count if inbound_count > 0 else None,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def run_reply_agent(
    session,
    deal=None,
    conversation_history: list[dict] | None = None,
) -> ReplyDecision:
    """Read the live LinkedIn conversation thread and return a structured reply decision.

    Parameters
    ----------
    session:
        The active AccountSession. Used for self_profile and campaign context.
    deal:
        Optional CRM Deal for enriched context (chat_summary, profile_summary).
        When None, operates on LinkedIn conversation alone.
    conversation_history:
        The complete visible thread from LinkedIn (list of message dicts).
        Each dict: {"is_outgoing": bool, "text": str, "sender": str, ...}

    Returns
    -------
    ReplyDecision
        Structured decision from the LLM.
    """
    if conversation_history is None:
        conversation_history = []

    public_id = getattr(getattr(deal, "lead", None), "public_identifier", None) or "lead"

    system_prompt = _render_system_prompt(session, deal, conversation_history)

    agent = Agent(
        get_llm_model(structured_output=True),
        output_type=ReplyDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(lambda: agent.run(system_prompt), structured_output=True).output
    if decision is None:
        raise RuntimeError(f"Reply agent returned unparseable response for {public_id}")

    logger.info("reply_agent for %s: action=%s", public_id, decision.action)
    return decision
