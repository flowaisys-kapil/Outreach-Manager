"""Add ``Deal.next_check_pending_at``; backfill existing PENDING deals to now()
so the first post-deploy planning cycle re-picks them up immediately.
"""
from django.db import migrations, models
from django.utils import timezone


def backfill_pending_deals(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    Deal.objects.filter(state="pending").update(
        next_check_pending_at=timezone.now(),
    )


def clear_next_check_pending_at(apps, schema_editor):
    Deal = apps.get_model("crm", "Deal")
    Deal.objects.update(next_check_pending_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0009_outcome"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="next_check_pending_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(
            backfill_pending_deals,
            clear_next_check_pending_at,
        ),
    ]
