# openoutreach/core/llm/__init__.py
"""Modular public API for the LLM subsystem."""
from __future__ import annotations

# Import time so that test patches on outreach_manager.core.llm.time.sleep succeed
import time

# Exception types & classification (errors.py)
from .errors import (
    ExceptionDetail,
    LLMFailure,
    LLMQuotaExhausted,
    _unwrap_exception_group,
    _inspect_exception,
    _classify,
    is_quota_error,
)

# Telemetry tracking (tracker.py)
from .tracker import (
    LLMUsageTracker,
    get_ai_usage_tracker,
    _ai_usage_tracker,
)

# Background runner event loop (runner.py)
from .runner import (
    _get_runner,
)

# Provider builders & registry (providers.py)
from .providers import (
    _build_openai,
    _build_anthropic,
    _build_google,
    _build_groq,
    _build_mistral,
    _build_cohere,
    _build_openai_compatible,
    _build_nvidia,
    _PROVIDER_BUILDERS,
)

# Model factory (model_factory.py)
from .model_factory import (
    get_llm_model,
)

# Pacing, parsing (utils.py)
from .utils import (
    _pace,
    _get_provider_name,
    split_model_id,
)

# Import test_provider_connection from settings/config to preserve public API
from outreach_manager.core.config_service import test_provider_connection

# Sync client runner (client.py)
from .client import (
    _APP_MAX_TRANSIENT_RETRIES,
    run_agent_sync,
)

# Public exports expected by the application
__all__ = [
    "get_llm_model",
    "run_agent_sync",
    "get_ai_usage_tracker",
    "LLMFailure",
    "LLMQuotaExhausted",
    "is_quota_error",
    "test_provider_connection",
    "split_model_id",
]
