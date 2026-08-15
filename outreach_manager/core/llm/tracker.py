# openoutreach/core/llm/tracker.py
"""Usage tracking and diagnostics telemetry for LLM calls."""
from __future__ import annotations

import threading


class LLMUsageTracker:
    """Thread-safe collector for AI usage telemetry within a session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self.primary_provider = ""
        self.fallback_provider = ""
        self.primary_calls = 0
        self.fallback_calls = 0
        self.successful_calls = 0
        self.failed_calls = 0
        self.structured_output_calls = 0
        self.retries = 0
        self.estimated_input_tokens = 0
        self.estimated_output_tokens = 0
        self._provider_telemetry: dict[str, dict] = {}

    def reset(self) -> None:
        with self._lock:
            self._reset_unlocked()

    def record_call_event(
        self,
        provider: str,
        success: bool,
        is_fallback: bool = False,
        structured_output: bool = False,
        is_retry: bool = False,
        response_time_ms: float = 0.0,
    ) -> None:
        from outreach_manager.core.config import get_config
        ai_cfg = get_config().ai
        with self._lock:
            if not self.primary_provider:
                self.primary_provider = ai_cfg.primary_provider or provider
            if not self.fallback_provider:
                self.fallback_provider = ai_cfg.fallback_provider or ""

            if is_fallback:
                self.fallback_calls += 1
            else:
                self.primary_calls += 1

            if success:
                self.successful_calls += 1
            else:
                self.failed_calls += 1

            if structured_output:
                self.structured_output_calls += 1

            if is_retry:
                self.retries += 1

            p_name = provider.lower().strip()
            if p_name not in self._provider_telemetry:
                self._provider_telemetry[p_name] = {
                    "total_calls": 0,
                    "successful_calls": 0,
                    "failure_count": 0,
                    "fallback_invocations": 0,
                    "response_times_ms": [],
                }
            p_stats = self._provider_telemetry[p_name]
            p_stats["total_calls"] += 1
            if success:
                p_stats["successful_calls"] += 1
            else:
                p_stats["failure_count"] += 1

            if is_fallback:
                p_stats["fallback_invocations"] += 1

            if response_time_ms > 0:
                p_stats["response_times_ms"].append(response_time_ms)

    def drain(self) -> dict:
        with self._lock:
            from outreach_manager.core.config import get_config
            ai_cfg = get_config().ai
            data = {
                "primary_provider": self.primary_provider or ai_cfg.primary_provider,
                "fallback_provider": self.fallback_provider or ai_cfg.fallback_provider,
                "primary_calls": self.primary_calls,
                "fallback_calls": self.fallback_calls,
                "successful_calls": self.successful_calls,
                "failed_calls": self.failed_calls,
                "structured_output_calls": self.structured_output_calls,
                "retries": self.retries,
                "estimated_input_tokens": self.estimated_input_tokens,
                "estimated_output_tokens": self.estimated_output_tokens,
                "provider_telemetry": self._provider_telemetry,
            }
            self._reset_unlocked()
            return data


_ai_usage_tracker = LLMUsageTracker()


def get_ai_usage_tracker() -> LLMUsageTracker:
    return _ai_usage_tracker
