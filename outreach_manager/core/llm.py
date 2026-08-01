"""LLM model factory + sync boundary for pydantic-ai.

Two public entry points:

- `get_llm_model(structured_output=False)` — builds a `pydantic_ai.Model` from
  `SiteConfig`, routing to the right provider.  When ``structured_output=True``
  the backup is only included in the ``FallbackModel`` if the env var
  ``BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE=true`` is set, because many small
  open-weight models (e.g. Llama-3.1-8B on Nvidia) cannot reliably honour
  pydantic-ai structured output contracts.

- `run_agent_sync(coro_fn, structured_output=False)` — drives a pydantic-ai
  coroutine factory to completion on a dedicated worker thread, with bounded
  app-level retry logic and clear failure classification.

Why a persistent worker thread (not `Agent.run_sync`, not `asyncio.run`):

- `Agent.run_sync` uses an anyio portal that leaves the caller thread's
  running-loop slot populated. Subsequent sync Playwright calls on the
  daemon thread then raise
  `"using Playwright Sync API inside the asyncio loop"`.
- `asyncio.run` per call closes its loop on exit. The openai / anthropic
  SDKs wrap `httpx.AsyncClient` in a subclass whose `__del__` does
  `get_running_loop().create_task(self.aclose())`. If GC fires the
  wrapper from call N during call N+1's loop, the cleanup task tries to
  close a transport bound to call N's now-closed loop →
  `RuntimeError: Event loop is closed`.

A single long-lived loop on a dedicated thread eliminates both: all HTTP
clients live on the same loop forever, and the runner thread's asyncio
slot stays inside this module — the caller thread is never touched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Awaitable, Callable, TypeVar

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Provider SDK-level retry counts.  These handle underlying HTTP jitter,
# 429 responses, and transient 5xx.  Keep small so app-level retry logic
# fires promptly rather than waiting for the SDK to exhaust its own loop.
_SDK_PRIMARY_RETRIES = 2
_SDK_FALLBACK_RETRIES = 1

# App-level retry cap for TRANSIENT failures (IOError / timeout / 5xx that
# slip through the SDK retries).  Hard quota / auth / capability errors switch
# to fallback immediately instead of retrying the same model.
_APP_MAX_TRANSIENT_RETRIES = 3

# Rate-limit delay tracking
_LAST_LLM_CALL = 0.0
_LLM_CALL_LOCK = threading.Lock()


# ── Exceptions ────────────────────────────────────────────────────────

class LLMFailure(RuntimeError):
    """Raised when every configured model (primary + fallback) has failed.

    ``category`` is one of:
      TRANSIENT          — network / timeout / 5xx (retried up to _APP_MAX_TRANSIENT_RETRIES)
      QUOTA_EXHAUSTED    — 429 / rate-limit
      AUTH               — bad API key / permission denied
      CAPABILITY         — model cannot produce the required output format
      VALIDATION         — pydantic validation on structured output repeatedly failed
      UNKNOWN            — exception type not recognised
    """
    def __init__(self, message: str, category: str = "UNKNOWN"):
        super().__init__(message)
        self.category = category


class LLMQuotaExhausted(LLMFailure):
    """Raised when an LLM provider returns HTTP 429 / rate limit / quota exhausted.

    Treated as a temporary infrastructure event rather than an application error.
    Workflows catch this exception to defer the affected Deal to a future session
    without raising stack traces or recording workflow failures.
    """
    def __init__(self, message: str, provider: str = "LLM Provider"):
        super().__init__(message, category="QUOTA_EXHAUSTED")
        self.provider = provider


def _classify(exc: Exception) -> str:
    """Map a provider exception to a failure category string."""
    type_name = type(exc).__name__
    module = getattr(type(exc), "__module__", "") or ""
    msg = str(exc).lower()

    # Auth / config
    if any(k in type_name for k in ("AuthenticationError", "PermissionDenied")):
        return "AUTH"
    if "auth" in msg and "key" in msg:
        return "AUTH"

    # Quota / rate-limit
    if (
        "RateLimitError" in type_name
        or "ResourceExhausted" in type_name
        or any(k in msg for k in ("429", "rate limit", "quota", "resource_exhausted", "resourceexhausted", "too many requests"))
    ):
        return "QUOTA_EXHAUSTED"

    # Structured-output / capability
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        return "VALIDATION"
    if "ModelBehaviorError" in type_name or "tool" in msg or "function call" in msg:
        return "CAPABILITY"

    # Transient network/IO
    if isinstance(exc, (IOError, TimeoutError, ConnectionError)):
        return "TRANSIENT"
    if any(k in type_name for k in ("Timeout", "ConnectError", "ReadTimeout")):
        return "TRANSIENT"
    if any(k in msg for k in ("timeout", "connection", "network", "503", "502")):
        return "TRANSIENT"

    return "UNKNOWN"


def is_quota_error(exc: Exception) -> bool:
    """Return True if *exc* represents an LLM provider quota/rate-limit (HTTP 429) event."""
    if isinstance(exc, LLMQuotaExhausted):
        return True
    if isinstance(exc, LLMFailure) and exc.category == "QUOTA_EXHAUSTED":
        return True
    return _classify(exc) == "QUOTA_EXHAUSTED"


def _get_provider_name() -> str:
    """Return human-readable active LLM provider name from SiteConfig."""
    try:
        from outreach_manager.core.models import SiteConfig
        cfg = SiteConfig.load()
        if cfg and cfg.ai_model:
            provider, model_name = split_model_id(cfg.ai_model)
            if provider == "google" or "gemini" in model_name.lower():
                return "Gemini"
            elif provider == "openai":
                return "OpenAI"
            elif provider == "anthropic":
                return "Anthropic"
            elif provider == "groq":
                return "Groq"
            return provider.capitalize()
    except Exception:
        pass
    return "LLM Provider"


# ── Async runner ─────────────────────────────────────────────────────

class _AgentRunner:
    """Owns one persistent asyncio loop on a dedicated daemon thread."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        threading.Thread(
            target=self._serve, args=(ready,), daemon=True, name="llm-runner",
        ).start()
        ready.wait()

    def _serve(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        ready.set()
        self._loop.run_forever()

    def run(self, coro: Awaitable[_T] | _T) -> _T:
        """Submit *coro* to the runner loop; block until it completes."""
        if not asyncio.iscoroutine(coro) and not isinstance(coro, asyncio.Future):
            return coro  # type: ignore[return-value]
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


_runner: _AgentRunner | None = None
_runner_lock = threading.Lock()


def _get_runner() -> _AgentRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = _AgentRunner()
    return _runner


def _pace() -> None:
    """Enforce per-call pacing to stay within free-tier rate limits."""
    delay = float(os.environ.get("LLM_RATE_LIMIT_DELAY", "3.0"))
    if delay > 0:
        global _LAST_LLM_CALL
        with _LLM_CALL_LOCK:
            now = time.monotonic()
            elapsed = now - _LAST_LLM_CALL
            if elapsed < delay:
                time.sleep(delay - elapsed)
            _LAST_LLM_CALL = time.monotonic()


def run_agent_sync(coro_fn: Callable[[], Awaitable[_T]], structured_output: bool = False) -> _T:
    """Drive *coro_fn()* on the dedicated LLM runner thread with bounded retry.

    ``coro_fn`` is a zero-argument callable that returns a fresh coroutine each
    time it is called (needed for retry: a consumed coroutine cannot be re-run).

    Failure classification:
      TRANSIENT    → retry up to _APP_MAX_TRANSIENT_RETRIES times with backoff
      QUOTA / CAPABILITY → stop retrying this call immediately; caller gets LLMFailure
      AUTH         → stop immediately
      VALIDATION   → re-ask once (pydantic-ai internal retry handles most cases)
      UNKNOWN      → treat as TRANSIENT
    """
    if not callable(coro_fn):
        if asyncio.iscoroutine(coro_fn):
            coro_fn.close()  # Clean up coroutine to prevent RuntimeWarning
        actual_type = type(coro_fn).__name__
        raise TypeError(
            f"run_agent_sync expected a zero-argument callable (e.g. lambda: agent.run(...)), "
            f"received {actual_type}"
        )

    last_exc: Exception | None = None
    transient_attempts = 0

    for attempt in range(_APP_MAX_TRANSIENT_RETRIES + 1):
        _pace()
        try:
            return _get_runner().run(coro_fn())
        except Exception as exc:
            category = _classify(exc)
            last_exc = exc

            if category == "AUTH":
                logger.error("[LLM] Auth/config failure — not retrying: %s", exc)
                raise LLMFailure(f"Auth failure: {exc}", category="AUTH") from exc

            if category == "QUOTA_EXHAUSTED":
                provider_name = _get_provider_name()
                logger.info(
                    "[INFO] LLM temporarily unavailable.\n  Provider: %s\n  Reason: Quota exhausted\n  Call deferred.",
                    provider_name,
                )
                raise LLMQuotaExhausted(f"LLM quota exhausted ({provider_name}): {exc}", provider=provider_name) from exc

            if category == "CAPABILITY":
                logger.warning("[LLM] CAPABILITY failure — stopping retries: %s", exc)
                raise LLMFailure(f"CAPABILITY: {exc}", category="CAPABILITY") from exc

            # TRANSIENT or UNKNOWN — bounded retry with backoff
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
        category=_classify(last_exc) if last_exc else "UNKNOWN",
    ) from last_exc


# ── Per-provider builders ────────────────────────────────────────────

def _build_openai(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel as OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    client = AsyncOpenAI(api_key=api_key, max_retries=max_retries, timeout=timeout)
    return OpenAIModel(model, provider=OpenAIProvider(openai_client=client))


def _build_anthropic(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    client = AsyncAnthropic(api_key=api_key, max_retries=max_retries, timeout=timeout)
    return AnthropicModel(model, provider=AnthropicProvider(anthropic_client=client))


def _build_google(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider
    return GoogleModel(model, provider=GoogleProvider(api_key=api_key))


def _build_groq(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from groq import AsyncGroq
    from pydantic_ai.models.groq import GroqModel
    from pydantic_ai.providers.groq import GroqProvider
    client = AsyncGroq(api_key=api_key, max_retries=max_retries, timeout=timeout)
    return GroqModel(model, provider=GroqProvider(groq_client=client))


def _build_mistral(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from pydantic_ai.models.mistral import MistralModel
    from pydantic_ai.providers.mistral import MistralProvider
    return MistralModel(model, provider=MistralProvider(api_key=api_key))


def _build_cohere(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    from pydantic_ai.models.cohere import CohereModel
    from pydantic_ai.providers.cohere import CohereProvider
    return CohereModel(model, provider=CohereProvider(api_key=api_key))


def _build_openai_compatible(model, api_key, api_base, max_retries=_SDK_PRIMARY_RETRIES, timeout=15.0):
    if not api_base:
        raise ValueError("LLM_API_BASE is required for the openai_compatible provider.")
    from pydantic_ai.models.openai import OpenAIChatModel as OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=api_base, api_key=api_key, max_retries=max_retries, timeout=timeout)
    return OpenAIModel(model, provider=OpenAIProvider(openai_client=client))


_PROVIDER_BUILDERS: dict[str, Callable] = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "google": _build_google,
    "groq": _build_groq,
    "mistral": _build_mistral,
    "cohere": _build_cohere,
    "openai_compatible": _build_openai_compatible,
}

# Bare-model fallbacks: only these prefixes are unambiguous enough to route
# without an explicit `provider:` prefix.
_LEGACY_MODEL_PREFIXES = {
    "gpt": "openai", "o1": "openai", "o3": "openai",
    "claude": "anthropic", "gemini": "google",
}


def split_model_id(ai_model: str) -> tuple[str, str]:
    """Split a `provider:model` identifier into ``(provider, model)``.

    A bare model name is accepted only when its prefix unambiguously implies a
    provider (see ``_LEGACY_MODEL_PREFIXES``); anything else raises so the
    misconfiguration surfaces instead of silently hitting the wrong API.
    """
    ai_model_stripped = ai_model.strip()
    if ":" in ai_model_stripped:
        provider, _, model = ai_model_stripped.partition(":")
        return provider, model

    lower_model = ai_model_stripped.lower()
    for prefix, provider in _LEGACY_MODEL_PREFIXES.items():
        if lower_model.startswith(prefix):
            if provider == "google":
                return provider, ai_model_stripped.replace(" ", "-").lower()
            return provider, ai_model_stripped

    if "gemini" in lower_model:
        return "google", ai_model_stripped.replace(" ", "-").lower()
    elif "gpt" in lower_model or "o1" in lower_model or "o3" in lower_model:
        return "openai", ai_model_stripped
    elif "claude" in lower_model:
        return "anthropic", ai_model_stripped

    raise ValueError(
        f"AI_MODEL {ai_model!r} has no provider prefix. "
        f"Use 'provider:model', e.g. 'google:{ai_model}'."
    )


# ── Model factory ────────────────────────────────────────────────────

def _validated_site_config():
    """Load `SiteConfig` and assert the required LLM fields are populated."""
    from outreach_manager.core.models import SiteConfig

    cfg = SiteConfig.load()
    if not cfg.llm_api_key:
        raise ValueError("LLM_API_KEY is not set in Site Configuration.")
    if not cfg.ai_model:
        raise ValueError("AI_MODEL is not set in Site Configuration.")
    return cfg


def get_llm_model(structured_output: bool = False):
    """Return a configured pydantic-ai ``Model`` for the current ``SiteConfig``.

    Parameters
    ----------
    structured_output:
        Set to ``True`` for agents that require pydantic structured output or
        tool-calling (e.g. follow-up agent, first-message generator, fact
        extractor).  When ``True``, the backup model is only included in the
        ``FallbackModel`` if the env var
        ``BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE=true`` is explicitly set.
        Many small open-weight models (e.g. Llama-3.1-8B on Nvidia) cannot
        reliably honour structured output contracts, so we must not silently
        route to them for structured workloads.

        When ``False`` (default), the backup is always included when configured,
        matching previous behaviour for plain-text agents.
    """
    from pydantic_ai.models.fallback import FallbackModel

    cfg = _validated_site_config()
    provider, model = split_model_id(cfg.ai_model)
    builder = _PROVIDER_BUILDERS.get(provider)
    if builder is None:
        raise ValueError(
            f"Unknown LLM provider {provider!r} in AI_MODEL {cfg.ai_model!r}. "
            f"Use one of: {', '.join(_PROVIDER_BUILDERS)}."
        )

    primary_model = builder(model, cfg.llm_api_key, cfg.llm_api_base,
                            max_retries=_SDK_PRIMARY_RETRIES)

    backup_key = os.environ.get("BACKUP_LLM_API_KEY", "")
    if not backup_key:
        return primary_model

    # Determine whether the backup is eligible given the workload type.
    backup_compatible = os.environ.get(
        "BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE", ""
    ).strip().lower() == "true"

    if structured_output and not backup_compatible:
        logger.debug(
            "[LLM] structured_output=True but backup is not flagged compatible "
            "(BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE). Using primary model only."
        )
        return primary_model

    backup_model_id = os.environ.get("BACKUP_AI_MODEL", "openai_compatible:meta/llama-3.1-8b-instruct")
    backup_base = os.environ.get("BACKUP_LLM_API_BASE", "https://integrate.api.nvidia.com/v1")

    backup_provider, backup_model_name = split_model_id(backup_model_id)
    backup_builder = _PROVIDER_BUILDERS.get(backup_provider)
    if backup_builder is None:
        logger.warning("[LLM] Backup provider %r not found — skipping fallback.", backup_provider)
        return primary_model

    backup_model = backup_builder(backup_model_name, backup_key, backup_base,
                                  max_retries=_SDK_FALLBACK_RETRIES)
    logger.info("[LLM] primary=%s, fallback=%s (structured_output=%s)",
                cfg.ai_model, backup_model_id, structured_output)
    return FallbackModel(primary_model, backup_model)
