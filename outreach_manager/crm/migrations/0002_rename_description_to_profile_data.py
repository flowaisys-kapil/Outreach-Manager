import json

from django.db import migrations, models


def convert_description_to_json(apps, schema_editor):
    """Parse JSON text in description into profile_data, set empty strings to None."""
    Lead = apps.get_model("crm", "Lead")
    for lead in Lead.objects.exclude(description="").iterator():
        lead.profile_data = json.loads(lead.description)
        lead.save(update_fields=["profile_data"])


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="profile_data",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(convert_description_to_json, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="lead",
            name="description",
        ),
    ]
