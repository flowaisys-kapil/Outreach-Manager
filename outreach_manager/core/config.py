# outreach_manager/core/config.py
"""Centralized Runtime Configuration System for OpenOutreach / Outreach Manager.

Single source of truth for all user-configurable settings. Supports hierarchical priority:
    Runtime Configuration Overrides -> Environment Variables -> Default Values
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

# Supported LLM providers matching llm module builders
SUPPORTED_AI_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "nvidia",
    "groq",
    "mistral",
    "cohere",
    "openai_compatible",
}

# Supported execution modes
SUPPORTED_EXECUTION_MODES = {"manual", "automatic"}

# Supported log levels
SUPPORTED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigurationError(ValueError):
    """Raised when a configuration value fails validation."""
    pass


# ── Sub-configurations ───────────────────────────────────────────────

@dataclass
class RuntimeConfig:
    """Runtime execution settings."""
    execution_mode: str = "manual"
    sessions_per_day: int = 1
    active_days: list[str] = field(
        default_factory=lambda: [
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
        ]
    )
    working_windows: list[dict[str, Any]] = field(
        default_factory=lambda: [{"start": 9, "end": 19}]
    )
    scheduler_horizon_hours: float = 24.0
    scheduler_immediate_mode: bool = False
    scheduler_min_delay_between_tasks: int = 30
    scheduler_max_delay_between_tasks: int = 60

    def validate(self) -> None:
        if self.execution_mode not in SUPPORTED_EXECUTION_MODES:
            raise ConfigurationError(
                f"Invalid execution_mode '{self.execution_mode}'. Must be one of: {sorted(SUPPORTED_EXECUTION_MODES)}"
            )
        if not (1 <= self.sessions_per_day <= 5):
            raise ConfigurationError(
                f"Invalid sessions_per_day {self.sessions_per_day}. Must be an integer between 1 and 5."
            )
        if self.scheduler_horizon_hours <= 0:
            raise ConfigurationError("scheduler_horizon_hours must be greater than 0.")
        if self.scheduler_min_delay_between_tasks < 0 or self.scheduler_max_delay_between_tasks < 0:
            raise ConfigurationError("Scheduler task delays must be non-negative integers.")
        if self.scheduler_min_delay_between_tasks > self.scheduler_max_delay_between_tasks:
            raise ConfigurationError(
                f"scheduler_min_delay_between_tasks ({self.scheduler_min_delay_between_tasks}) "
                f"cannot exceed scheduler_max_delay_between_tasks ({self.scheduler_max_delay_between_tasks})."
            )


@dataclass
class BrowserConfig:
    """Browser launch & interaction settings."""
    use_cdp: bool = True
    cdp_url: str = "http://127.0.0.1:9222"
    profile_dir: str = "data/chrome_profile"
    recovery_enabled: bool = True

    def validate(self) -> None:
        if not self.cdp_url or not isinstance(self.cdp_url, str):
            raise ConfigurationError("cdp_url must be a non-empty string.")


@dataclass
class AIConfig:
    """LLM provider and model settings."""
    primary_provider: str = "google"
    primary_model: str = "google:gemini-2.0-flash"
    primary_api_key: str = ""
    primary_api_base: str | None = None
    fallback_provider: str | None = "openai_compatible"
    fallback_model: str | None = "openai_compatible:meta/llama-3.1-8b-instruct"
    fallback_api_key: str | None = ""
    fallback_api_base: str | None = "https://integrate.api.nvidia.com/v1"
    rate_limit_delay: float = 3.0
    backup_structured_output_compatible: bool = False

    def validate(self) -> None:
        if self.primary_provider not in SUPPORTED_AI_PROVIDERS:
            raise ConfigurationError(
                f"Invalid primary_provider '{self.primary_provider}'. Must be one of: {sorted(SUPPORTED_AI_PROVIDERS)}"
            )
        if self.fallback_provider and self.fallback_provider not in SUPPORTED_AI_PROVIDERS:
            raise ConfigurationError(
                f"Invalid fallback_provider '{self.fallback_provider}'. Must be one of: {sorted(SUPPORTED_AI_PROVIDERS)}"
            )
        if self.rate_limit_delay < 0:
            raise ConfigurationError("rate_limit_delay must be a non-negative float.")


SUPPORTED_WORKFLOWS = {
    "connect",
    "reply",
    "follow_up",
    "first_message",
    "check_pending",
    "extract_leads",
    "email",
}

WORKFLOW_ALIASES = {
    "reply_unread": "reply",
    "extract": "extract_leads",
}


@dataclass
class WorkflowsConfig:
    """Campaign & workflow limits and execution controls."""
    enabled_workflows: list[str] = field(
        default_factory=lambda: [
            "connect",
            "reply",
            "follow_up",
            "first_message",
            "check_pending",
            "extract_leads",
            "email",
        ]
    )
    connect_daily_limit: int = 20
    reply_daily_limit: int = 40
    follow_up_daily_limit: int = 30
    first_message_daily_limit: int = 20
    check_pending_daily_limit: int = 50
    extract_leads_daily_limit: int = 100
    email_daily_limit: int = 30
    max_connects_per_run: int = 10
    max_follow_ups_per_run: int = 12
    max_transient_retries: int = 3

    def canonical_name(self, name: str) -> str:
        n = name.lower().strip()
        return WORKFLOW_ALIASES.get(n, n)

    def is_enabled(self, workflow: str) -> bool:
        canon = self.canonical_name(workflow)
        enabled_set = {self.canonical_name(w) for w in self.enabled_workflows}
        return canon in enabled_set

    def get_daily_limit(self, workflow: str) -> int:
        canon = self.canonical_name(workflow)
        if canon == "connect":
            return self.connect_daily_limit
        elif canon == "reply":
            return self.reply_daily_limit
        elif canon == "follow_up":
            return self.follow_up_daily_limit
        elif canon == "first_message":
            return self.first_message_daily_limit
        elif canon == "check_pending":
            return self.check_pending_daily_limit
        elif canon == "extract_leads":
            return self.extract_leads_daily_limit
        elif canon == "email":
            return self.email_daily_limit
        return 999999

    def validate(self) -> None:
        seen = set()
        for wf in self.enabled_workflows:
            canon = self.canonical_name(wf)
            if canon not in SUPPORTED_WORKFLOWS:
                raise ConfigurationError(
                    f"Invalid workflow name '{wf}'. Must be one of: {sorted(SUPPORTED_WORKFLOWS)}"
                )
            if canon in seen:
                raise ConfigurationError(f"Duplicate workflow definition found for '{wf}'.")
            seen.add(canon)

        limits = [
            ("connect_daily_limit", self.connect_daily_limit),
            ("reply_daily_limit", self.reply_daily_limit),
            ("follow_up_daily_limit", self.follow_up_daily_limit),
            ("first_message_daily_limit", self.first_message_daily_limit),
            ("check_pending_daily_limit", self.check_pending_daily_limit),
            ("extract_leads_daily_limit", self.extract_leads_daily_limit),
            ("email_daily_limit", self.email_daily_limit),
        ]
        for name, limit in limits:
            if limit <= 0:
                raise ConfigurationError(f"Daily workflow limit '{name}' must be a positive integer.")

        if self.max_connects_per_run <= 0 or self.max_follow_ups_per_run <= 0:
            raise ConfigurationError("Per-run task caps must be positive integers.")
        if self.max_transient_retries < 0:
            raise ConfigurationError("max_transient_retries cannot be negative.")


@dataclass
class DiagnosticsConfig:
    """Logging and telemetry settings."""
    log_level: str = "DEBUG"
    session_history_enabled: bool = True
    ai_usage_tracking_enabled: bool = True
    notifications_enabled: bool = True
    notify_on_success: bool = True
    notify_on_warning: bool = True
    notify_on_failure: bool = True
    notify_on_info: bool = False
    notification_delivery_mode: str = "toast"
    color_enabled: bool = True

    def validate(self) -> None:
        if self.log_level.upper() not in SUPPORTED_LOG_LEVELS:
            raise ConfigurationError(
                f"Invalid log_level '{self.log_level}'. Must be one of: {sorted(SUPPORTED_LOG_LEVELS)}"
            )
        if self.notification_delivery_mode not in ("toast", "console_only", "disabled"):
            raise ConfigurationError(
                f"Invalid notification_delivery_mode '{self.notification_delivery_mode}'. Must be one of: ['toast', 'console_only', 'disabled']"
            )


# ── Root AppConfig ───────────────────────────────────────────────────

@dataclass
class AppConfig:
    """Root configuration object uniting all configuration domains."""
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    workflows: WorkflowsConfig = field(default_factory=WorkflowsConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)

    def validate(self) -> None:
        """Validate all sub-configurations; fail fast on invalid parameters."""
        self.runtime.validate()
        self.browser.validate()
        self.ai.validate()
        self.workflows.validate()
        self.diagnostics.validate()


# ── Helpers & Priority Resolution ─────────────────────────────────────

def _str_to_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes", "on")


def _infer_provider(model_str: str, default: str = "google") -> str:
    """Infer provider name from model string."""
    if not model_str:
        return default
    model_str = model_str.strip()
    if ":" in model_str:
        return model_str.partition(":")[0]
    lower = model_str.lower()
    if "gemini" in lower:
        return "google"
    elif "gpt" in lower or "o1" in lower or "o3" in lower:
        return "openai"
    elif "claude" in lower:
        return "anthropic"
    return default


def load_config(
    env: Mapping[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Construct and validate an AppConfig instance following strict priority:

    Overrides (Runtime) -> Environment Variables -> Default Values
    """
    if env is None:
        env = os.environ
    if overrides is None:
        overrides = {}

    # Extract sub-overrides if present
    rt_overrides = overrides.get("runtime", {})
    br_overrides = overrides.get("browser", {})
    ai_overrides = overrides.get("ai", {})
    wf_overrides = overrides.get("workflows", {})
    dg_overrides = overrides.get("diagnostics", {})

    # 1. Runtime Config
    exec_mode = rt_overrides.get("execution_mode", env.get("EXECUTION_MODE", "manual")).lower()
    sessions_day = int(rt_overrides.get("sessions_per_day", env.get("SESSIONS_PER_DAY", "1")))
    
    sch_horizon = float(rt_overrides.get("scheduler_horizon_hours", env.get("SCHEDULER_HORIZON_HOURS", "24.0")))
    sch_immediate = _str_to_bool(
        rt_overrides.get("scheduler_immediate_mode", env.get("SCHEDULER_IMMEDIATE_MODE")),
        default=False,
    )
    sch_min_delay = int(
        rt_overrides.get("scheduler_min_delay_between_tasks", env.get("SCHEDULER_MIN_DELAY_BETWEEN_TASKS", "30"))
    )
    sch_max_delay = int(
        rt_overrides.get("scheduler_max_delay_between_tasks", env.get("SCHEDULER_MAX_DELAY_BETWEEN_TASKS", "60"))
    )

    active_days_default = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    env_days = env.get("ACTIVE_DAYS")
    if env_days:
        active_days_default = [d.strip().lower() for d in env_days.split(",") if d.strip()]

    env_start = env.get("WORKING_HOURS_START")
    env_end = env.get("WORKING_HOURS_END")
    working_windows_default = [{"start": int(env_start), "end": int(env_end)}] if (env_start and env_end) else [{"start": 9, "end": 19}]

    runtime_cfg = RuntimeConfig(
        execution_mode=exec_mode,
        sessions_per_day=sessions_day,
        active_days=rt_overrides.get("active_days", active_days_default),
        working_windows=rt_overrides.get("working_windows", working_windows_default),
        scheduler_horizon_hours=sch_horizon,
        scheduler_immediate_mode=sch_immediate,
        scheduler_min_delay_between_tasks=sch_min_delay,
        scheduler_max_delay_between_tasks=sch_max_delay,
    )

    # 2. Browser Config
    use_cdp = _str_to_bool(br_overrides.get("use_cdp", env.get("USE_CDP")), default=True)
    cdp_url = br_overrides.get("cdp_url", env.get("CDP_URL", "http://127.0.0.1:9222"))
    prof_dir = br_overrides.get("profile_dir", env.get("CHROME_PROFILE_DIR", "data/chrome_profile"))
    rec_enabled = _str_to_bool(br_overrides.get("recovery_enabled", env.get("BROWSER_RECOVERY_ENABLED")), default=True)

    browser_cfg = BrowserConfig(
        use_cdp=use_cdp,
        cdp_url=cdp_url,
        profile_dir=prof_dir,
        recovery_enabled=rec_enabled,
    )

    # 3. AI Config
    pri_model = ai_overrides.get("primary_model", env.get("AI_MODEL", "google:gemini-2.0-flash"))
    pri_provider = ai_overrides.get(
        "primary_provider", env.get("PRIMARY_AI_PROVIDER", _infer_provider(pri_model, "google"))
    )
    pri_key = ai_overrides.get("primary_api_key", env.get("LLM_API_KEY", ""))
    pri_base = ai_overrides.get("primary_api_base", env.get("LLM_API_BASE", None))

    fb_model = ai_overrides.get(
        "fallback_model", env.get("BACKUP_AI_MODEL", "openai_compatible:meta/llama-3.1-8b-instruct")
    )
    fb_provider = ai_overrides.get(
        "fallback_provider", env.get("FALLBACK_AI_PROVIDER", _infer_provider(fb_model, "openai_compatible"))
    )
    fb_key = ai_overrides.get("fallback_api_key", env.get("BACKUP_LLM_API_KEY", ""))
    fb_base = ai_overrides.get("fallback_api_base", env.get("BACKUP_LLM_API_BASE", "https://integrate.api.nvidia.com/v1"))

    rl_delay = float(ai_overrides.get("rate_limit_delay", env.get("LLM_RATE_LIMIT_DELAY", "3.0")))
    fb_struct_compat = _str_to_bool(
        ai_overrides.get("backup_structured_output_compatible", env.get("BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE", env.get("BACKUP_STRUCTURED_OUTPUT_COMPATIBLE"))),
        default=False,
    )

    ai_cfg = AIConfig(
        primary_provider=pri_provider,
        primary_model=pri_model,
        primary_api_key=pri_key,
        primary_api_base=pri_base,
        fallback_provider=fb_provider,
        fallback_model=fb_model,
        fallback_api_key=fb_key,
        fallback_api_base=fb_base,
        rate_limit_delay=rl_delay,
        backup_structured_output_compatible=fb_struct_compat,
    )

    # 4. Workflows Config
    conn_limit = int(wf_overrides.get("connect_daily_limit", env.get("DEFAULT_CONNECT_DAILY_LIMIT", "20")))
    rep_limit = int(wf_overrides.get("reply_daily_limit", env.get("DEFAULT_REPLY_DAILY_LIMIT", "40")))
    fol_limit = int(wf_overrides.get("follow_up_daily_limit", env.get("DEFAULT_FOLLOW_UP_DAILY_LIMIT", "30")))
    fm_limit = int(wf_overrides.get("first_message_daily_limit", env.get("DEFAULT_FIRST_MESSAGE_DAILY_LIMIT", "20")))
    cp_limit = int(wf_overrides.get("check_pending_daily_limit", env.get("DEFAULT_CHECK_PENDING_DAILY_LIMIT", "50")))
    el_limit = int(wf_overrides.get("extract_leads_daily_limit", env.get("DEFAULT_EXTRACT_LEADS_DAILY_LIMIT", "100")))
    em_limit = int(wf_overrides.get("email_daily_limit", env.get("DEFAULT_EMAIL_DAILY_LIMIT", "30")))

    max_conn_run = int(wf_overrides.get("max_connects_per_run", env.get("SCHEDULER_MAX_CONNECTS_PER_RUN", "10")))
    max_fol_run = int(wf_overrides.get("max_follow_ups_per_run", env.get("SCHEDULER_MAX_FOLLOW_UPS_PER_RUN", "12")))
    max_retries = int(wf_overrides.get("max_transient_retries", env.get("MAX_TRANSIENT_RETRIES", "3")))

    default_enabled = ["connect", "reply", "follow_up", "first_message", "check_pending", "extract_leads", "email"]
    env_enabled_wf = env.get("ENABLED_WORKFLOWS")
    if env_enabled_wf:
        default_enabled = [w.strip() for w in env_enabled_wf.split(",") if w.strip()]
    enabled_wf = wf_overrides.get("enabled_workflows", default_enabled)

    workflows_cfg = WorkflowsConfig(
        enabled_workflows=enabled_wf,
        connect_daily_limit=conn_limit,
        reply_daily_limit=rep_limit,
        follow_up_daily_limit=fol_limit,
        first_message_daily_limit=fm_limit,
        check_pending_daily_limit=cp_limit,
        extract_leads_daily_limit=el_limit,
        email_daily_limit=em_limit,
        max_connects_per_run=max_conn_run,
        max_follow_ups_per_run=max_fol_run,
        max_transient_retries=max_retries,
    )

    # 5. Diagnostics Config
    l_level = dg_overrides.get("log_level", env.get("LOG_LEVEL", "DEBUG")).upper()
    sess_hist = _str_to_bool(dg_overrides.get("session_history_enabled", env.get("SESSION_HISTORY_ENABLED")), default=True)
    ai_track = _str_to_bool(dg_overrides.get("ai_usage_tracking_enabled", env.get("AI_USAGE_TRACKING_ENABLED")), default=True)
    notif_enabled = _str_to_bool(dg_overrides.get("notifications_enabled", env.get("NOTIFICATIONS_ENABLED")), default=True)
    notif_succ = _str_to_bool(dg_overrides.get("notify_on_success", env.get("NOTIFY_ON_SUCCESS")), default=True)
    notif_warn = _str_to_bool(dg_overrides.get("notify_on_warning", env.get("NOTIFY_ON_WARNING")), default=True)
    notif_fail = _str_to_bool(dg_overrides.get("notify_on_failure", env.get("NOTIFY_ON_FAILURE")), default=True)
    notif_info = _str_to_bool(dg_overrides.get("notify_on_info", env.get("NOTIFY_ON_INFO")), default=False)
    notif_mode = str(dg_overrides.get("notification_delivery_mode", env.get("NOTIFICATION_DELIVERY_MODE", "toast"))).lower()
    env_color = env.get("COLOR_LOGS_ENABLED")
    default_color = _str_to_bool(env_color, default=("NO_COLOR" not in env)) if env_color is not None else ("NO_COLOR" not in env)
    color_on = _str_to_bool(dg_overrides.get("color_enabled"), default=default_color) if "color_enabled" in dg_overrides else default_color

    diagnostics_cfg = DiagnosticsConfig(
        log_level=l_level,
        session_history_enabled=sess_hist,
        ai_usage_tracking_enabled=ai_track,
        notifications_enabled=notif_enabled,
        notify_on_success=notif_succ,
        notify_on_warning=notif_warn,
        notify_on_failure=notif_fail,
        notify_on_info=notif_info,
        notification_delivery_mode=notif_mode,
        color_enabled=color_on,
    )

    config = AppConfig(
        runtime=runtime_cfg,
        browser=browser_cfg,
        ai=ai_cfg,
        workflows=workflows_cfg,
        diagnostics=diagnostics_cfg,
    )

    config.validate()
    return config


# ── Global Singleton Access ──────────────────────────────────────────

_global_config: AppConfig | None = None
_cached_env: dict[str, str] | None = None
_custom_env: Mapping[str, str] | None = None
_runtime_overrides: dict[str, Any] | None = None


def set_runtime_overrides(overrides: dict[str, Any] | None) -> None:
    """Set explicit runtime configuration overrides and invalidate cache."""
    global _runtime_overrides, _global_config
    _runtime_overrides = overrides
    _global_config = None


def get_config() -> AppConfig:
    """Return the global AppConfig singleton."""
    global _global_config, _cached_env
    env_to_use = _custom_env if _custom_env is not None else os.environ
    current_env = dict(env_to_use)
    if _global_config is None or _cached_env != current_env:
        _cached_env = current_env
        _global_config = load_config(env=current_env, overrides=_runtime_overrides)
    return _global_config


def reset_config(
    env: Mapping[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Reset and reload the global shared AppConfig singleton."""
    global _global_config, _cached_env, _custom_env, _runtime_overrides
    _custom_env = env
    _runtime_overrides = overrides
    
    env_to_use = env if env is not None else os.environ
    _cached_env = dict(env_to_use)
    _global_config = load_config(env=env_to_use, overrides=overrides)
    return _global_config
