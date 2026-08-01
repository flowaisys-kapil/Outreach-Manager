from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("crm", "0007_drop_legacy_lead_fields"),
    ]

    operations = [
        migrations.RunSQL("VACUUM;", reverse_sql=migrations.RunSQL.noop),
    ]
