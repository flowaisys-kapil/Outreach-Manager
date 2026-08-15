# openoutreach/core/agents/first_message.py
"""First message generator: composes personalized intro 1st LinkedIn message for a deal.

Single LLM call with structured output returning message content.
No decision logic (wait/mark_completed/send_message) — the workflow has already
decided that a first message should be sent.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from outreach_manager.core.agents.prompt import base_context, render
from outreach_manager.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class FirstMessageResult(BaseModel):
    """Structured output for the first message generator agent."""

    message: str = Field(
        description="The short, highly personalized 1-2 sentence first message for this prospect. Do NOT use template brackets or placeholders like [name] or [company]."
    )


def generate_first_message(session, deal) -> str:
    """Generate a personalized first LinkedIn message for `deal` from its summaries and campaign docs."""
    system_prompt = render("first_message_agent.j2", **base_context(session, deal))

    agent = Agent(
        get_llm_model(structured_output=True),
        output_type=FirstMessageResult,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    result = run_agent_sync(lambda: agent.run(system_prompt), structured_output=True).output
    if result is None or not result.message:
        raise ValueError(f"first_message generator returned no message content for {deal.lead.public_identifier}")

    logger.info("generate_first_message for %s: %s", deal.lead.public_identifier, result.message)
    return result.message
