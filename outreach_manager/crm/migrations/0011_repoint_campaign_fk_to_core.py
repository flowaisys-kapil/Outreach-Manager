# State-only: Deal.campaign now points at core.Campaign (same table, same
# column — the model moved apps, the database is untouched).
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0010_deal_next_check_pending_at"),
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name="deal",
                    name="campaign",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deals",
                        to="core.campaign",
                    ),
                ),
            ],
        ),
    ]
