# openoutreach/core/llm/client.py
"""Client execution and synchronization entry point for LLM agent calls."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

from outreach_manager.core.config import get_config
from .utils import _pace, _get_provider_name
from .runner import _get_runner
from .tracker import _ai_usage_tracker
from .errors import (
    _unwrap_exception_group,
    _inspect_exception,
    LLMFailure,
    LLMQuotaExhausted,
)

_T = TypeVar("_T")
logger = logging.getLogger(__name__)

_APP_MAX_TRANSIENT_RETRIES = 3


def run_agent_sync(coro_fn: Callable[[], Awaitable[_T]], structured_output: bool = False) -> _T:
    """Drive *coro_fn()* on the dedicated LLM runner thread with bounded retry."""
    if not callable(coro_fn):
        if asyncio.iscoroutine(coro_fn):
            coro_fn.close()
        actual_type = type(coro_fn).__name__
        raise TypeError(
            f"run_agent_sync expected a zero-argument callable (e.g. lambda: agent.run(...)), "
            f"received {actual_type}"
        )

    ai_cfg = get_config().ai
    tracking_enabled = get_config().diagnostics.ai_usage_tracking_enabled
    active_provider = ai_cfg.primary_provider or "google"
    active_model = ai_cfg.primary_model or "gemini-2.5-flash"

    logger.info("Provider: %s\nModel: %s\n\nInference started...", active_provider, active_model)

    last_exc: Exception | None = None
    transient_attempts = 0

    for attempt in range(_APP_MAX_TRANSIENT_RETRIES + 1):
        _pace()
        start_t = time.monotonic()
        try:
            res = _get_runner().run(coro_fn())
            elapsed_s = time.monotonic() - start_t
            logger.info("Inference completed successfully.\n\nDuration: %.1fs", elapsed_s)
            if tracking_enabled:
                _ai_usage_tracker.record_call_event(
                    provider=active_provider,
                    success=True,
                    structured_output=structured_output,
                    is_retry=(attempt > 0),
                    response_time_ms=elapsed_s * 1000.0,
                )
            return res
        except Exception as exc:
            elapsed_s = time.monotonic() - start_t
            if tracking_enabled:
                _ai_usage_tracker.record_call_event(
                    provider=active_provider,
                    success=False,
                    structured_output=structured_output,
                    is_retry=(attempt > 0),
                    response_time_ms=elapsed_s * 1000.0,
                )

            last_exc = exc
            unwrapped = _unwrap_exception_group(exc)
            primary_detail = _inspect_exception(unwrapped[0])
            fallback_detail = _inspect_exception(unwrapped[1]) if len(unwrapped) > 1 else None

            # Quota exhaustion (429) is a temporary operational deferral event
            if primary_detail.category == "QUOTA_EXHAUSTED" or (fallback_detail and fallback_detail.category == "QUOTA_EXHAUSTED"):
                provider_name = _get_provider_name()
                logger.info(
                    "[INFO] LLM temporarily unavailable.\n  Provider: %s\n  Reason: Quota exhausted\n  Call deferred.",
                    provider_name,
                )
                raise LLMQuotaExhausted(f"LLM quota exhausted ({provider_name}): {primary_detail.message}", provider=provider_name) from exc

            # Format explicit provider diagnostic log block for actual failures
            fallback_log = (
                f"\n  Fallback Provider:"
                f"\n    Provider: {ai_cfg.fallback_provider or 'N/A'}"
                f"\n    Model: {ai_cfg.fallback_model or 'N/A'}"
                f"\n    Status: {fallback_detail.status_code or 'N/A'}"
                f"\n    Category: {fallback_detail.category}"
                f"\n    Reason: {fallback_detail.reason}"
            ) if fallback_detail else "\n  Fallback Provider: None configured or not invoked"

            logger.error(
                "[LLM Failure Diagnostics]\n"
                "  Primary Provider:\n"
                "    Provider: %s\n"
                "    Model: %s\n"
                "    Status: %s\n"
                "    Category: %s\n"
                "    Reason: %s%s",
                ai_cfg.primary_provider,
                ai_cfg.primary_model,
                primary_detail.status_code or "N/A",
                primary_detail.category,
                primary_detail.reason,
                fallback_log,
            )

            # Check for non-retryable permanent errors
            is_permanent = not primary_detail.is_retryable or (fallback_detail and not fallback_detail.is_retryable)

            if is_permanent:
                logger.error(
                    "[LLM] Permanent failure (%s — %s) — not retrying.",
                    primary_detail.category, primary_detail.reason,
                )
                raise LLMFailure(
                    f"{primary_detail.reason}: {primary_detail.message}",
                    category=primary_detail.category,
                ) from exc

            # Transient errors: retry up to max_transient_retries
            transient_attempts += 1
            if transient_attempts > _APP_MAX_TRANSIENT_RETRIES:
                break
            backoff = 2 ** transient_attempts
            logger.warning(
                "[LLM] Transient failure (attempt %d/%d), retrying in %ds: %s",
                transient_attempts, _APP_MAX_TRANSIENT_RETRIES, backoff, exc,
            )
            time.sleep(backoff)

    raise LLMFailure(
        f"All {_APP_MAX_TRANSIENT_RETRIES + 1} attempts failed. Last error: {last_exc}",
        category=_inspect_exception(last_exc).category if last_exc else "UNKNOWN",
    ) from last_exc
