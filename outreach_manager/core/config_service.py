# outreach_manager/core/config_service.py
"""Centralized Configuration Service for Outreach Manager.

Single owner of all configuration persistence. No other component should
read or write `.env` directly.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent.parent   # repo root
_ENV_PATH  = _ROOT / ".env"
_BACKUP_PATH = _ROOT / "data" / "config_backup.json"

# ── Managed keys ──────────────────────────────────────────────────────────────

MANAGED_KEYS: frozenset[str] = frozenset({
    # Runtime
    "EXECUTION_MODE",
    "SESSIONS_PER_DAY",
    "ACTIVE_DAYS",
    "WORKING_HOURS_START",
    "WORKING_HOURS_END",
    "SCHEDULER_HORIZON_HOURS",
    "SCHEDULER_IMMEDIATE_MODE",
    "SCHEDULER_MIN_DELAY_BETWEEN_TASKS",
    "SCHEDULER_MAX_DELAY_BETWEEN_TASKS",
    "SCHEDULER_MAX_CONNECTS_PER_RUN",
    "SCHEDULER_MAX_FOLLOW_UPS_PER_RUN",
    # Browser
    "USE_CDP",
    "CDP_URL",
    # AI — primary
    "PRIMARY_AI_PROVIDER",
    "AI_MODEL",
    "LLM_API_KEY",
    "LLM_API_BASE",
    "LLM_RATE_LIMIT_DELAY",
    # AI — fallback
    "FALLBACK_AI_PROVIDER",
    "BACKUP_AI_MODEL",
    "BACKUP_LLM_API_KEY",
    "BACKUP_LLM_API_BASE",
    "BACKUP_STRUCTURED_OUTPUT_COMPATIBLE",
    "BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE",
    # Workflows
    "ENABLED_WORKFLOWS",
    "DEFAULT_CONNECT_DAILY_LIMIT",
    "DEFAULT_REPLY_DAILY_LIMIT",
    "DEFAULT_FOLLOW_UP_DAILY_LIMIT",
    "DEFAULT_FIRST_MESSAGE_DAILY_LIMIT",
    "DEFAULT_CHECK_PENDING_DAILY_LIMIT",
    "DEFAULT_EXTRACT_LEADS_DAILY_LIMIT",
    "DEFAULT_EMAIL_DAILY_LIMIT",
    # Diagnostics
    "SESSION_HISTORY_ENABLED",
    "AI_USAGE_TRACKING_ENABLED",
    "NOTIFICATIONS_ENABLED",
    "NOTIFY_ON_SUCCESS",
    "NOTIFY_ON_WARNING",
    "NOTIFY_ON_FAILURE",
    "NOTIFY_ON_INFO",
    "NOTIFICATION_DELIVERY_MODE",
    "COLOR_LOGS_ENABLED",
})

# Keys that contain secrets — never logged by value
_SECRET_KEYS: frozenset[str] = frozenset({
    "LLM_API_KEY",
    "BACKUP_LLM_API_KEY",
})


# ── Low-level .env I/O ────────────────────────────────────────────────────────

def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Parse a .env file and return a ``{key: value}`` dict."""
    p = Path(path) if path else _ENV_PATH
    data: dict[str, str] = {}
    if not p.exists():
        return data
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, _, v = stripped.partition("=")
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                data[k] = v
    except OSError as exc:
        logger.warning("ConfigurationService: failed to read %s — %s", p, exc)
    return data


def _read_raw_lines(path: Path) -> list[str]:
    """Return raw lines of a file, preserving comments and blank lines."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readlines()
    except OSError:
        return []


def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* atomically."""
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".env_tmp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_updates_into_lines(
    raw_lines: list[str],
    updates: dict[str, str],
) -> str:
    """Merge *updates* into existing .env raw lines."""
    written: set[str] = set()
    out: list[str] = []

    for line in raw_lines:
        stripped = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue

        k, _, _ = stripped.partition("=")
        k = k.strip()

        if k in updates:
            out.append(f"{k}={updates[k]}\n")
            written.add(k)
        else:
            out.append(line)

    for k, v in updates.items():
        if k not in written:
            out.append(f"{k}={v}\n")

    return "".join(out)


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_updates(updates: dict[str, str]) -> None:
    """Validate *updates* by merging them with current env and running load_config."""
    from outreach_manager.core.config import load_config, ConfigurationError

    merged_env = dict(os.environ)
    merged_env.update(updates)
    load_config(env=merged_env)


# ── Backup ────────────────────────────────────────────────────────────────────

def _write_backup(data: dict[str, str]) -> None:
    """Persist *data* to backup file."""
    _BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe_data = {k: (v if k not in _SECRET_KEYS else "***") for k, v in data.items()}
    try:
        _write_atomic(
            _BACKUP_PATH,
            json.dumps({"config": data, "note": "Last successfully validated configuration."}, indent=2),
        )
        logger.info("ConfigurationService: Backup updated (%d keys).", len(data))
    except Exception as exc:
        logger.warning("ConfigurationService: Failed to write backup — %s", exc)


def _load_backup() -> dict[str, str] | None:
    """Load and return the backup dict, or None if unavailable/corrupt."""
    if not _BACKUP_PATH.exists():
        return None
    try:
        with open(_BACKUP_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("config", {})
    except Exception as exc:
        logger.warning("ConfigurationService: Cannot read backup — %s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def read_current_config(path: Path | str | None = None) -> dict[str, str]:
    """Return the full {key: value} dict from the live .env file."""
    all_vals = load_env_file(path)
    return {k: v for k, v in all_vals.items() if k in MANAGED_KEYS}


def save(updates: dict[str, str], env_path: Path | str | None = None) -> None:
    """Validate, atomically write .env, update backup, reload runtime."""
    path = Path(env_path) if env_path else _ENV_PATH

    _validate_updates(updates)
    raw_lines = _read_raw_lines(path)
    new_content = _merge_updates_into_lines(raw_lines, updates)
    _write_atomic(path, new_content)

    managed_subset = {k: v for k, v in updates.items() if k in MANAGED_KEYS}
    _write_backup(managed_subset)
    reload_runtime(path)

    secret_count = sum(1 for k in updates if k in _SECRET_KEYS)
    suffix = f", {secret_count} secret key(s) redacted" if secret_count else ""
    logger.info(
        "ConfigurationService: Configuration saved — %d keys updated%s.",
        len(updates),
        suffix,
    )


def reload_runtime(env_path: Path | str | None = None) -> None:
    """Re-read .env into os.environ and reset the AppConfig singleton."""
    from outreach_manager.core.config import reset_config

    path = Path(env_path) if env_path else _ENV_PATH
    data = load_env_file(path)
    os.environ.update(data)
    reset_config(env=os.environ)
    logger.info("ConfigurationService: Runtime configuration reloaded.")


def restore_from_backup(env_path: Path | str | None = None) -> bool:
    """Restore .env from backup."""
    path = Path(env_path) if env_path else _ENV_PATH
    data = _load_backup()
    if not data:
        logger.error("ConfigurationService: No usable backup found — cannot restore.")
        return False

    try:
        raw_lines = _read_raw_lines(path)
        new_content = _merge_updates_into_lines(raw_lines, data)
        _write_atomic(path, new_content)
        logger.warning("ConfigurationService: Backup restored to %s (%d keys).", path, len(data))
        return True
    except Exception as exc:
        logger.error("ConfigurationService: Backup restore failed — %s", exc)
        return False


def startup_load(env_path: Path | str | None = None) -> None:
    """Called at application startup."""
    from outreach_manager.core.config import load_config, reset_config, ConfigurationError

    path = Path(env_path) if env_path else _ENV_PATH

    env_data = load_env_file(path)
    merged = dict(os.environ)
    merged.update(env_data)
    try:
        load_config(env=merged)
        os.environ.update(env_data)
        reset_config(env=os.environ)
        logger.info("ConfigurationService: Configuration loaded from %s.", path)
        return
    except (ConfigurationError, Exception) as primary_exc:
        logger.warning("ConfigurationService: .env validation failed (%s). Attempting backup restore…", primary_exc)

    restored = restore_from_backup(path)
    if restored:
        env_data = load_env_file(path)
        merged = dict(os.environ)
        merged.update(env_data)
        try:
            load_config(env=merged)
            os.environ.update(env_data)
            reset_config(env=os.environ)
            logger.warning("ConfigurationService: Running on restored backup configuration.")
            return
        except (ConfigurationError, Exception) as backup_exc:
            logger.error("ConfigurationService: Backup configuration also invalid — %s", backup_exc)

    msg = (
        "\n\n"
        "  ╔══════════════════════════════════════════════════════════╗\n"
        "  ║         OUTREACH MANAGER — CONFIGURATION ERROR           ║\n"
        "  ╠══════════════════════════════════════════════════════════╣\n"
        "  ║  Both .env and config_backup.json contain invalid        ║\n"
        "  ║  configuration and cannot be recovered automatically.    ║\n"
        "  ║                                                          ║\n"
        "  ║  Please review your .env file and correct any errors,    ║\n"
        "  ║  then restart Outreach Manager.                          ║\n"
        "  ╚══════════════════════════════════════════════════════════╝\n"
    )
    logger.critical("ConfigurationService: Startup aborted — no valid configuration available.")
    print(msg, file=sys.stderr)
    sys.exit(1)


def test_provider_connection(
    provider: str,
    model: str = "",
    api_key: str = "",
    api_base: str | None = None,
) -> tuple[bool, str]:
    """Test connection to specified AI provider."""
    import outreach_manager.core.llm as llm
    p_name = provider.lower().strip()
    
    builder_name = f"_build_{p_name}"
    builder = getattr(llm, builder_name, None)
    if not builder:
        return False, f"Unsupported provider '{provider}'"

    if not api_key:
        from outreach_manager.core.config import get_config
        cfg = get_config().ai
        if p_name == cfg.primary_provider:
            api_key = cfg.primary_api_key
        elif p_name == cfg.fallback_provider:
            api_key = cfg.fallback_api_key or ""

    if not api_key and p_name not in ("google", "openai_compatible"):
        return False, f"No API key provided for provider '{provider}'"

    try:
        test_model = model or f"{p_name}:default"
        built_model = builder(model=test_model, api_key=api_key, api_base=api_base, timeout=5.0)
        if hasattr(built_model, "invoke"):
            built_model.invoke("Ping")
        return True, "Connected"
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        return False, f"Connection failed: {err_msg}"


test_provider_connection.__test__ = False
