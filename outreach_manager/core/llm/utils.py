# openoutreach/core/llm/utils.py
"""Generic utility functions for model identifiers, provider display names, and pacing."""
from __future__ import annotations

import threading
import time


_LAST_LLM_CALL = 0.0
_LLM_CALL_LOCK = threading.Lock()


def _pace() -> None:
    """Enforce per-call pacing to stay within free-tier rate limits."""
    from outreach_manager.core.config import get_config
    delay = get_config().ai.rate_limit_delay
    if delay > 0:
        global _LAST_LLM_CALL
        with _LLM_CALL_LOCK:
            now = time.monotonic()
            elapsed = now - _LAST_LLM_CALL
            if elapsed < delay:
                time.sleep(delay - elapsed)
            _LAST_LLM_CALL = time.monotonic()


def _get_provider_name() -> str:
    """Return human-readable active LLM provider name from centralized config."""
    try:
        from outreach_manager.core.config import get_config
        ai_cfg = get_config().ai
        p = ai_cfg.primary_provider.lower() if ai_cfg.primary_provider else ""
        if p == "google" or "gemini" in ai_cfg.primary_model.lower():
            return "Gemini"
        elif p == "openai" or "gpt" in ai_cfg.primary_model.lower():
            return "OpenAI"
        elif p == "anthropic" or "claude" in ai_cfg.primary_model.lower():
            return "Anthropic"
        elif p in ("nvidia", "openai_compatible") or "llama" in ai_cfg.primary_model.lower():
            return "NVIDIA"
        elif p:
            return p.capitalize()
    except Exception:
        pass
    return "LLM Provider"


def split_model_id(ai_model: str) -> tuple[str, str]:
    """Split a `provider:model` identifier into ``(provider, model)``.

    Requires the canonical provider:model format.
    """
    ai_model_stripped = ai_model.strip()
    if ":" in ai_model_stripped:
        provider, _, model = ai_model_stripped.partition(":")
        return provider, model

    raise ValueError(
        f"AI_MODEL {ai_model!r} has no provider prefix. "
        f"Use 'provider:model', e.g. 'google:{ai_model}'."
    )
