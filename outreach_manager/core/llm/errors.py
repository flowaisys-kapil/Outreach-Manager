# openoutreach/core/llm/errors.py
"""Exception types and classification logic for the LLM subsystem."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExceptionDetail:
    status_code: int | None
    category: str  # AUTH, NOT_FOUND, BAD_REQUEST, QUOTA_EXHAUSTED, TRANSIENT, VALIDATION, CAPABILITY, UNKNOWN
    is_retryable: bool
    reason: str
    message: str


class LLMFailure(RuntimeError):
    """Raised when every configured model (primary + fallback) has failed."""
    def __init__(self, message: str, category: str = "UNKNOWN"):
        super().__init__(message)
        self.category = category


class LLMQuotaExhausted(LLMFailure):
    """Raised when an LLM provider returns HTTP 429 / rate limit / quota exhausted."""
    def __init__(self, message: str, provider: str = "LLM Provider"):
        super().__init__(message, category="QUOTA_EXHAUSTED")
        self.provider = provider


def _unwrap_exception_group(exc: Exception) -> list[Exception]:
    """Unwrap FallbackExceptionGroup / ExceptionGroup / nested causes into a flat list of provider exceptions."""
    if hasattr(exc, "exceptions") and isinstance(getattr(exc, "exceptions"), (list, tuple)):
        result = []
        for inner in getattr(exc, "exceptions"):
            if isinstance(inner, Exception):
                result.extend(_unwrap_exception_group(inner))
        if result:
            return result
    if hasattr(exc, "__cause__") and isinstance(getattr(exc, "__cause__"), Exception):
        return [getattr(exc, "__cause__")]
    return [exc]


def _inspect_exception(exc: Exception) -> ExceptionDetail:
    """Analyze an exception to extract HTTP status code, category, retryability, and failure reason."""
    type_name = type(exc).__name__
    msg = str(exc)
    msg_lower = msg.lower()

    status_code: int | None = None
    for attr in ("status_code", "status", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            status_code = val
            break
        elif isinstance(val, str) and val.isdigit():
            status_code = int(val)
            break

    if status_code is None:
        for code_str in ("401", "403", "404", "400", "429", "500", "502", "503", "504"):
            if f"status {code_str}" in msg_lower or f"code {code_str}" in msg_lower or f"http {code_str}" in msg_lower:
                status_code = int(code_str)
                break

    if (
        status_code in (401, 403)
        or any(k in type_name for k in ("AuthenticationError", "PermissionDenied", "Unauthenticated"))
        or any(k in msg_lower for k in ("invalid api key", "unauthorized", "api_key", "permission denied", "invalid_api_key", "api key invalid", "authentication failed"))
    ):
        return ExceptionDetail(
            status_code=status_code or 401,
            category="AUTH",
            is_retryable=False,
            reason="API Key Invalid or Unauthorized",
            message=msg,
        )

    if (
        status_code == 404
        or any(k in type_name for k in ("NotFoundError", "NotFound"))
        or any(k in msg_lower for k in ("model_not_found", "model does not exist", "not found", "unknown model"))
    ):
        return ExceptionDetail(
            status_code=status_code or 404,
            category="NOT_FOUND",
            is_retryable=False,
            reason="Model or Endpoint Not Found",
            message=msg,
        )

    if (
        status_code == 400
        or any(k in type_name for k in ("BadRequestError", "InvalidArgument"))
        or any(k in msg_lower for k in ("invalid request", "invalid_argument", "bad request"))
    ):
        return ExceptionDetail(
            status_code=status_code or 400,
            category="BAD_REQUEST",
            is_retryable=False,
            reason="Malformed or Invalid Request",
            message=msg,
        )

    if (
        status_code == 429
        or "RateLimitError" in type_name
        or "ResourceExhausted" in type_name
        or any(k in msg_lower for k in ("429", "rate limit", "quota", "resource_exhausted", "too many requests"))
    ):
        return ExceptionDetail(
            status_code=status_code or 429,
            category="QUOTA_EXHAUSTED",
            is_retryable=True,
            reason="Rate Limit / Quota Exhausted",
            message=msg,
        )

    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        return ExceptionDetail(
            status_code=status_code,
            category="VALIDATION",
            is_retryable=False,
            reason="Structured Output Validation Failed",
            message=msg,
        )

    if "ModelBehaviorError" in type_name or "tool" in msg_lower or "function call" in msg_lower:
        return ExceptionDetail(
            status_code=status_code,
            category="CAPABILITY",
            is_retryable=False,
            reason="Model Capability / Behavior Error",
            message=msg,
        )

    if (
        (status_code and 500 <= status_code <= 599)
        or isinstance(exc, (IOError, TimeoutError, ConnectionError))
        or any(k in type_name for k in ("Timeout", "ConnectError", "ReadTimeout", "ServiceUnavailable", "InternalServerError"))
        or any(k in msg_lower for k in ("timeout", "connection", "network", "503", "502", "500", "server error"))
    ):
        return ExceptionDetail(
            status_code=status_code or 500,
            category="TRANSIENT",
            is_retryable=True,
            reason="Transient Network or Server Error",
            message=msg,
        )

    return ExceptionDetail(
        status_code=status_code,
        category="UNKNOWN",
        is_retryable=False,
        reason=f"Unclassified Error ({type_name})",
        message=msg,
    )


def _classify(exc: Exception) -> str:
    """Map a provider exception to a failure category string."""
    unwrapped = _unwrap_exception_group(exc)
    return _inspect_exception(unwrapped[0]).category


def is_quota_error(exc: Exception) -> bool:
    """Return True if *exc* represents an LLM provider quota/rate-limit (HTTP 429) event."""
    if isinstance(exc, LLMQuotaExhausted):
        return True
    if isinstance(exc, LLMFailure) and exc.category == "QUOTA_EXHAUSTED":
        return True
    return _classify(exc) == "QUOTA_EXHAUSTED"
