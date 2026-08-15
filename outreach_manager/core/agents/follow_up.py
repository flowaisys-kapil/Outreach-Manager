# openoutreach/core/agents/follow_up.py
"""Follow-up agent: reads conversation, returns a structured decision.

Single LLM call with structured output — no tool-calling loop.
The handler in tasks/follow_up.py executes the decision.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator
from pydantic_ai import Agent

from outreach_manager.core.agents.prompt import _format_facts
from outreach_manager.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


# A non-empty, non-whitespace-only string — used to make `message` structurally
# required for `send_message` decisions rather than relying solely on a
# post-validation check that the LLM can silently violate.
_NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class FollowUpDecision(BaseModel):
    """Structured output from the follow-up agent.

    Valid combinations:
      send_message  — message is required (non-empty string)
      mark_completed — outcome is required
      wait          — neither required
    """

    action: Literal["send_message", "mark_completed", "wait"] = Field(
        description=(
            "What to do next for this lead. "
            "'send_message' requires a non-empty message string. "
            "'mark_completed' requires an outcome value. "
            "'wait' requires neither."
        ),
    )
    # message is typed as _NonEmptyStr so the schema itself advertises that
    # an empty/whitespace string is not a valid message, reducing LLM
    # miscomprehension without any post-validation tricks.
    message: _NonEmptyStr | None = Field(
        default=None,
        description=(
            "The exact message text to send. "
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
        description="Hours until next follow-up. Always required — you decide the pace.",
    )

    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError(
                "action='send_message' requires a non-empty 'message' field. "
                "Provide the actual message text or choose 'wait' instead."
            )
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError(
                "action='mark_completed' requires an 'outcome' value. "
                "Choose one of: converted, not_interested, wrong_fit, no_budget, "
                "has_solution, bad_timing, unresponsive."
            )
        return self


# Number of trailing verbatim messages the agent sees alongside the rolling
# chat_summary. Older turns live in the summary fact list; the recency window
# preserves literal phrasing for the turns that matter most when composing
# the next reply.
RECENT_MESSAGES_WINDOW = 6


def _humanize_age(when: datetime, now: datetime) -> str:
    """Render `when` as a coarse age relative to `now` (e.g. ``3d ago``)."""
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _format_recent_messages(messages: list, now: datetime) -> str:
    """Render ChatMessage rows or dicts as a timestamped transcript."""
    if not messages:
        return "No recent messages."
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
    return "\n".join(lines) or "No recent messages."


def _days_since_last_outgoing(messages: list, now: datetime) -> int | None:
    """Whole days since the most recent outgoing message, or None if there are none."""
    timestamps = []
    for m in messages:
        is_out = m.get("is_outgoing") if isinstance(m, dict) else getattr(m, "is_outgoing", False)
        ts = m.get("timestamp") if isinstance(m, dict) else getattr(m, "creation_date", None)
        if is_out and ts and isinstance(ts, datetime):
            timestamps.append(ts)
    if not timestamps:
        return None
    return max((now - max(timestamps)).days, 0)


def _count_unanswered_outgoing(messages: list) -> int:
    """Trailing run of outgoing messages with no lead reply after them."""
    count = 0
    for m in reversed(messages):
        is_out = m.get("is_outgoing") if isinstance(m, dict) else getattr(m, "is_outgoing", False)
        if is_out:
            count += 1
        else:
            break
    return count


def _log_chat_facts(public_id: str, deal) -> None:
    """Log the mem0 chat facts the agent is working with."""
    if not deal:
        return
    chat_facts = (deal.chat_summary or {}).get("facts", [])
    if not chat_facts:
        return
    lines = [f"chat facts for {public_id}:"]
    lines.extend(f"  • {f}" for f in chat_facts)
    logger.info("\n".join(lines))


def _load_recent_messages(deal, limit: int = RECENT_MESSAGES_WINDOW) -> list:
    """Last `limit` ChatMessages for `deal`, in chronological order."""
    if not deal:
        return []
    from outreach_manager.chat.models import ChatMessage

    qs = ChatMessage.objects.filter(deal=deal).order_by("-creation_date", "-pk")[:limit]
    return list(reversed(list(qs)))


def _render_system_prompt(session, deal, recent_messages: list) -> str:
    """Render the LinkedIn follow-up prompt: shared base + the LinkedIn-only extras."""
    from django.utils import timezone
    from outreach_manager.core.agents.prompt import base_context, render

    now = timezone.now()
    chat_facts = _format_facts(deal.chat_summary) if deal else "(none yet)"
    return render(
        "follow_up_agent.j2",
        **base_context(session, deal),
        contact_email=getattr(getattr(session, "linkedin_profile", None), "linkedin_username", ""),
        chat_summary=chat_facts,
        recent_messages=_format_recent_messages(recent_messages, now),
        today=now.strftime("%Y-%m-%d"),
        days_since_last_outgoing=_days_since_last_outgoing(recent_messages, now),
        unanswered_outgoing=_count_unanswered_outgoing(recent_messages),
    )


def run_follow_up_agent(session, deal=None, conversation_history: list[dict] | None = None) -> FollowUpDecision:
    """Read the LinkedIn conversation and return a structured follow-up decision.

    Accepts `conversation_history` directly from LinkedIn live thread (LinkedIn-as-Source-of-Truth).
    Can operate cleanly when `deal=None` if CRM record is missing.
    """
    public_id = getattr(getattr(deal, "lead", None), "public_identifier", None) or "lead"

    if conversation_history is not None:
        recent = conversation_history[-RECENT_MESSAGES_WINDOW:]
    else:
        if deal is not None:
            from outreach_manager.linkedin.db.chat import sync_conversation
            sync_conversation(session, public_id)
            deal.refresh_from_db(fields=["chat_summary", "profile_summary"])
            _log_chat_facts(public_id, deal)
        recent = _load_recent_messages(deal)

    system_prompt = _render_system_prompt(session, deal, recent)

    agent = Agent(
        get_llm_model(structured_output=True),
        output_type=FollowUpDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(lambda: agent.run(system_prompt), structured_output=True).output
    if decision is None:
        raise RuntimeError(f"LLM returned unparseable response for follow-up of {public_id}")

    logger.info("follow_up agent for %s: %s", public_id, decision.action)
    return decision


if __name__ == "__main__":
    from outreach_manager.crm.models import Deal
    from outreach_manager.linkedin.browser.registry import cli_parser, cli_session
    from outreach_manager.core.db.summaries import materialize_profile_summary_if_missing
    from outreach_manager.core.models import Task

    parser = cli_parser("Run the follow-up agent for a profile")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", help="Public identifier of the target profile")
    group.add_argument("--task-id", type=int, help="Task ID to run the agent for")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    if args.task_id:
        task = Task.objects.get(pk=args.task_id)
        public_id = task.payload["public_id"]
        campaign_id = task.payload["campaign_id"]
        from outreach_manager.core.models import Campaign
        campaign = Campaign.objects.get(pk=campaign_id)
        session.campaign = campaign
    else:
        public_id = args.profile

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if not deal:
        logger.error("No Deal found for %s", public_id)
        raise SystemExit(1)

    logger.info("Running follow-up agent as %s for %s", session, public_id)
    logger.info("Campaign: %s", session.campaign)

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    logger.info("Chat facts: %s", _format_facts(deal.chat_summary))
    logger.info("Action: %s", decision.action)
    if decision.message:
        logger.info("Message: %s", decision.message)
    if decision.outcome:
        logger.info("Outcome: %s", decision.outcome)
    logger.info("Follow-up in: %sh", decision.follow_up_hours)
