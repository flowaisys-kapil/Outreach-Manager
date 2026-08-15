# openoutreach/core/models.py
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class SiteConfig(models.Model):
    """Singleton model for global site configuration (LLM keys, etc.)."""

    # The model is a pydantic-ai model identifier in `provider:model` form
    # (e.g. ``anthropic:claude-sonnet-4-5-20250929``, ``openai:gpt-4o``,
    # ``groq:llama-3.3-70b``). The provider lives inside this single string —
    # there is no separate provider field to drift out of sync. A bare model
    # name whose prefix is unambiguous (``gpt``/``o1``/``o3``→openai,
    # ``claude``→anthropic, ``gemini``→google) is also accepted; everything
    # else must carry an explicit prefix. See core/llm.py:split_model_id.
    ai_model = models.CharField(
        max_length=200, blank=True, default="",
        help_text="provider:model, e.g. anthropic:claude-sonnet-4-5-20250929",
    )
    llm_api_key = models.CharField(max_length=500, blank=True, default="")
    # Only consulted for the openai_compatible provider (OpenRouter / Together / Ollama / vLLM).
    llm_api_base = models.CharField(max_length=500, blank=True, default="")

    # BetterContact email-finder key; blank disables enrichment (see emails/bettercontact.py).
    bettercontact_api_key = models.CharField(max_length=500, blank=True, default="")

    # Task Pacing manual overrides (replaces simulated time overrides)
    simulated_task = models.CharField(max_length=50, blank=True, default="")
    override_expires_at = models.DateTimeField(null=True, blank=True)
    last_config_save = models.DateTimeField(null=True, blank=True)

    # Central contacts service (see openoutreach/contacts/). The token is earned
    # on the first contribution and persisted here — never in the repo; blank
    # means "not registered yet" (resolve misses until the first give-back mints
    # it). The URL is blank by default (falls back to DEFAULT_CONTACTS_API_URL).
    contacts_api_token = models.CharField(max_length=500, blank=True, default="")
    contacts_api_url = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

    def __str__(self):
        return "Site Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SiteConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        try:
            from outreach_manager.core.config import get_config
            cfg = get_config().ai
            if cfg.primary_model:
                obj.ai_model = cfg.primary_model
            if cfg.primary_api_key:
                obj.llm_api_key = cfg.primary_api_key
            if cfg.primary_api_base is not None:
                obj.llm_api_base = cfg.primary_api_base or ""
        except Exception:
            pass
        return obj


class Campaign(models.Model):
    name = models.CharField(max_length=200, unique=True)
    users = models.ManyToManyField(User, blank=True, related_name="campaigns")
    product_docs = models.TextField(blank=True)
    campaign_objective = models.TextField(blank=True)
    booking_link = models.URLField(max_length=500, blank=True)
    seed_public_ids = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.name


class TaskQuerySet(models.QuerySet):
    def pending(self):
        """Pending tasks, EMAIL first, then oldest-scheduled first.

        Email outranks the LinkedIn channels so a *ready* send always preempts
        a ready connect/follow_up/check_pending — on startup and on every claim.
        Email slots are always scheduled ``now``, so ranking them first never
        makes ``seconds_to_next`` oversleep a sooner LinkedIn task."""
        email_first = models.Case(
            models.When(task_type=Task.TaskType.EMAIL, then=models.Value(0)),
            default=models.Value(1),
            output_field=models.IntegerField(),
        )
        return self.filter(status=Task.Status.PENDING).order_by(email_first, "scheduled_at")

    def claim_next(self) -> "Task | None":
        return self.pending().filter(scheduled_at__lte=timezone.now()).first()

    def claim_next_of_type(self, task_type: "Task.TaskType", campaign) -> "Task | None":
        """Claim the oldest due PENDING task of a specific type for a campaign."""
        return (
            self.pending()
            .filter(
                task_type=task_type,
                scheduled_at__lte=timezone.now(),
                payload__campaign_id=campaign.pk,
            )
            .first()
        )

    def seconds_to_next(self) -> float | None:
        """Seconds until the next pending task, or None if queue is empty."""
        next_task = self.pending().only("scheduled_at").first()
        if next_task is None:
            return None
        return max((next_task.scheduled_at - timezone.now()).total_seconds(), 0)


class Task(models.Model):
    class TaskType(models.TextChoices):
        CONNECT = "connect"
        CHECK_PENDING = "check_pending"
        FOLLOW_UP = "follow_up"
        EMAIL = "email"
        REPLY_UNREAD = "reply_unread"
        FIRST_MESSAGE = "first_message"
        EXTRACT_LEADS = "extract_leads"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    scheduled_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "scheduled_at"],
                name="core_task_status_sched_idx",
            ),
        ]

    def __str__(self):
        return f"{self.task_type} [{self.status}] scheduled={self.scheduled_at}"

    def mark_running(self):
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self):
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])

    def mark_failed(self):
        self.status = self.Status.FAILED
        self.save(update_fields=["status"])


class SessionHistory(models.Model):
    """Authoritative persistent record of completed outreach sessions."""
    session_id = models.CharField(max_length=100, unique=True)
    start_time = models.DateTimeField()
    finish_time = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(default=0.0)
    execution_mode = models.CharField(max_length=50, default="manual")
    workflows_executed = models.JSONField(default=list, blank=True)
    workflows_disabled = models.JSONField(default=list, blank=True)
    workflows_skipped = models.JSONField(default=list, blank=True)
    actions_completed = models.IntegerField(default=0)
    deal_errors = models.IntegerField(default=0)
    workflow_errors = models.IntegerField(default=0)
    fatal_errors = models.IntegerField(default=0)
    browser_recoveries = models.IntegerField(default=0)
    llm_deferrals = models.IntegerField(default=0)
    diagnostics_generated = models.IntegerField(default=0)
    total_errors = models.IntegerField(default=0)
    status = models.CharField(max_length=50, default="Completed")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Session History"
        verbose_name_plural = "Session Histories"
        ordering = ["-start_time"]

    def __str__(self):
        return f"Session {self.session_id} [{self.status}]"


class AIUsageLog(models.Model):
    """Structured AI usage telemetry per outreach session."""
    session = models.ForeignKey(
        SessionHistory,
        on_delete=models.CASCADE,
        related_name="ai_usage",
        null=True,
        blank=True,
    )
    primary_provider = models.CharField(max_length=50, default="")
    fallback_provider = models.CharField(max_length=50, default="")
    primary_calls = models.IntegerField(default=0)
    fallback_calls = models.IntegerField(default=0)
    successful_calls = models.IntegerField(default=0)
    failed_calls = models.IntegerField(default=0)
    structured_output_calls = models.IntegerField(default=0)
    retries = models.IntegerField(default=0)
    estimated_input_tokens = models.IntegerField(default=0)
    estimated_output_tokens = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI Usage Log"
        verbose_name_plural = "AI Usage Logs"

    def __str__(self):
        return f"AIUsage primary={self.primary_provider} calls={self.primary_calls + self.fallback_calls}"


class ProviderHealth(models.Model):
    """Rolling health statistics and performance counters per AI provider."""
    provider_name = models.CharField(max_length=50, unique=True)
    total_calls = models.IntegerField(default=0)
    successful_calls = models.IntegerField(default=0)
    failure_count = models.IntegerField(default=0)
    fallback_invocations = models.IntegerField(default=0)
    avg_response_time_ms = models.FloatField(default=0.0)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Provider Health"
        verbose_name_plural = "Provider Healths"

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 100.0
        return (self.successful_calls / self.total_calls) * 100.0

    @classmethod
    def record_batch(
        cls,
        provider_name: str,
        total_calls: int,
        successful_calls: int,
        failure_count: int,
        fallback_invocations: int = 0,
        response_times_ms: list[float] | None = None,
    ) -> "ProviderHealth":
        """Persist aggregated provider health statistics in a single DB write."""
        p_name = provider_name.lower().strip()
        obj, _ = cls.objects.get_or_create(provider_name=p_name)
        obj.total_calls += total_calls
        obj.successful_calls += successful_calls
        obj.failure_count += failure_count
        obj.fallback_invocations += fallback_invocations

        if response_times_ms:
            for rt in response_times_ms:
                if rt > 0:
                    if obj.avg_response_time_ms == 0.0:
                        obj.avg_response_time_ms = float(rt)
                    else:
                        obj.avg_response_time_ms = round(
                            0.8 * obj.avg_response_time_ms + 0.2 * rt, 2
                        )

        obj.save()
        return obj

    @classmethod
    def record_call(
        cls,
        provider_name: str,
        success: bool,
        response_time_ms: float = 0.0,
        fallback: bool = False,
    ) -> "ProviderHealth":
        """Record a single invocation (convenience wrapper over record_batch)."""
        return cls.record_batch(
            provider_name=provider_name,
            total_calls=1,
            successful_calls=1 if success else 0,
            failure_count=0 if success else 1,
            fallback_invocations=1 if fallback else 0,
            response_times_ms=[response_time_ms] if response_time_ms > 0 else [],
        )

    def __str__(self):
        return f"Provider {self.provider_name}: {self.success_rate:.1f}% success ({self.total_calls} calls)"
