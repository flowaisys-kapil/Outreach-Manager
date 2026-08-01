# outreach_manager/crm/models/event_log.py
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class EventLog(models.Model):
    class EventType(models.TextChoices):
        CONNECT_REQUESTED = "connect_requested", _("Connection Requested")
        CONNECT_ACCEPTED = "connect_accepted", _("Connection Accepted")
        MESSAGE_SENT = "message_sent", _("Message Sent")
        MESSAGE_RECEIVED = "message_received", _("Message Received")
        EMAIL_SENT = "email_sent", _("Email Sent")

    campaign = models.ForeignKey(
        "core.Campaign",
        on_delete=models.CASCADE,
        related_name="event_logs",
        verbose_name=_("Campaign")
    )
    deal = models.ForeignKey(
        "crm.Deal",
        on_delete=models.CASCADE,
        related_name="event_logs",
        null=True,
        blank=True,
        verbose_name=_("Deal")
    )
    event_type = models.CharField(
        max_length=50,
        choices=EventType.choices,
        verbose_name=_("Event Type")
    )
    detail = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Detail")
    )
    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Created At")
    )

    class Meta:
        verbose_name = _("Event Log")
        verbose_name_plural = _("Event Logs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.created_at.strftime('%Y-%m-%d %H:%M')}"
