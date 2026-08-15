# openoutreach/core/agents/prompt.py
"""Shared prompt generator for the outreach agents.

Both entrypoints — the LinkedIn follow-up agent and the email opener — render
from one Jinja base (``_outreach_base.j2``: identity, product docs, lead summary,
Mom Test strategy, shared rules) and fill only their channel-specific blocks. The
base context here is the shared half; each entrypoint adds its own extras.
"""
from __future__ import annotations

import jinja2

from outreach_manager.core.conf import PROMPTS_DIR

_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))

# ── Campaign-level context cache ─────────────────────────────────────────────
# `product_docs`, `campaign_objective`, and `booking_link` rarely change during
# a run.  Cache the formatted strings so we avoid repeated DB reads and string
# builds on every single LLM call.
# Key: campaign.pk  — this cache is process-scoped (one daemon run).  When the
# daemon restarts after an admin edit the cache is rebuilt automatically.
_campaign_ctx_cache: dict[int, dict] = {}


def _campaign_stable_ctx(campaign) -> dict:
    """Return the stable, campaign-level prompt variables, using a process cache.

    Reads the DB once per campaign per process lifetime.  Restart the daemon to
    pick up campaign setting changes.
    """
    pk = campaign.pk
    if pk not in _campaign_ctx_cache:
        _campaign_ctx_cache[pk] = {
            "product_docs": campaign.product_docs or "",
            "campaign_objective": campaign.campaign_objective or "",
            "booking_link": campaign.booking_link or "",
        }
    return _campaign_ctx_cache[pk]


def render(template_name: str, **context) -> str:
    """Render a prompt template by name from the shared prompts dir."""
    return _ENV.get_template(template_name).render(**context)


def base_context(session, deal=None) -> dict:
    """The channel-agnostic prompt variables shared by every outreach entrypoint."""
    campaign = getattr(deal, "campaign", None) or getattr(session, "campaign", None)
    self_prof = getattr(session, "self_profile", {}) or {}
    self_name = (
        f"{self_prof.get('first_name', '')} {self_prof.get('last_name', '')}".strip()
        or getattr(getattr(session, "django_user", None), "username", "me")
    )
    profile_summary = _format_facts(deal.profile_summary) if deal else "(none yet)"
    ctx = {
        "self_name": self_name,
        "profile_summary": profile_summary,
    }
    if campaign:
        ctx.update(_campaign_stable_ctx(campaign))
    else:
        ctx.update({
            "product_docs": "",
            "campaign_objective": "",
            "booking_link": "",
        })
    return ctx


def _format_facts(summary: dict | None) -> str:
    """Render a `{facts: [...]}` summary blob as a bullet list."""
    if not isinstance(summary, dict):
        return "(none yet)"
    facts = summary.get("facts") or []
    if not facts or not isinstance(facts, list):
        return "(none yet)"
    return "\n".join(f"- {f}" for f in facts)
