from django.conf import settings
from django.db import models
from django.template.defaultfilters import truncatechars
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.urls import reverse


class ChatMessage(models.Model):

    class Meta:
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        constraints = [
            models.UniqueConstraint(
                fields=["deal", "linkedin_urn"],
                name="uniq_deal_linkedin_urn",
            ),
        ]

    deal = models.ForeignKey(
        "crm.Deal",
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name=_("Deal"),
    )

    content = models.TextField(
        blank=True, default='',
        verbose_name=_("Message")
    )    
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, blank=True, null=True, on_delete=models.CASCADE,
        verbose_name=_("Owner"),
        related_name="%(app_label)s_%(class)s_owner_related",
    )
    answer_to = models.ForeignKey(
        'self', blank=True, null=True, on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_answer_to_related",
        verbose_name=_("answer to")
    )
    topic = models.ForeignKey(
        'self', blank=True, null=True, on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_topic_related",
    )
    creation_date = models.DateTimeField(
        default=timezone.now,
        verbose_name=_("Creation date")
    )
    linkedin_urn = models.CharField(
        max_length=300,
        verbose_name=_("LinkedIn message URN"),
        help_text=_("entityUrn from Voyager API, used for dedup (per deal)"),
    )
    is_outgoing = models.BooleanField(
        default=True,
        verbose_name=_("Outgoing"),
        help_text=_("True if sent by us, False if received"),
    )
    def __str__(self):
        return f'{truncatechars(self.content, 70)}'

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            try:
                from outreach_manager.crm.models.event_log import EventLog
                event_type = EventLog.EventType.MESSAGE_SENT if self.is_outgoing else EventLog.EventType.MESSAGE_RECEIVED
                sender = "Me" if self.is_outgoing else "Lead"
                EventLog.objects.create(
                    campaign=self.deal.campaign,
                    deal=self.deal,
                    event_type=event_type,
                    detail=f"[{sender}] {self.content}",
                    created_at=self.creation_date or timezone.now()
                )
            except Exception:
                pass

    def get_absolute_url(self):
        return reverse(f'admin:chat_{self._meta.model_name}_change', args=[str(self.id)])

