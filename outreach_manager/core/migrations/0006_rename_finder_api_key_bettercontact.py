from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_siteconfig_contacts_api_token_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="siteconfig",
            old_name="finder_api_key",
            new_name="bettercontact_api_key",
        ),
    ]
