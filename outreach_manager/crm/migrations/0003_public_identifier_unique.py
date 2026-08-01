"""Backfill empty public_identifier/linkedin_url, then enforce unique + non-nullable."""
import logging
from urllib.parse import quote, urlparse, unquote

from django.db import migrations, models

logger = logging.getLogger(__name__)


def _url_to_public_id(url):
    if not url:
        return None
    parts = urlparse(url.strip()).path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "in":
        return None
    return unquote(parts[1])


def _public_id_to_url(public_id):
    if not public_id:
        return ""
    return f"https://www.linkedin.com/in/{quote(public_id.strip('/'), safe='')}/"


def backfill(apps, schema_editor):
    Lead = apps.get_model("crm", "Lead")

    # Set public_identifier from URL for all leads (fixes stale /in/me/ markers).
    for lead in Lead.objects.all():
        pid = _url_to_public_id(lead.linkedin_url)
        if pid and pid != lead.public_identifier:
            logger.debug("Backfill: Lead %d public_identifier='%s' → '%s'", lead.pk, lead.public_identifier, pid)
            lead.public_identifier = pid
            lead.save(update_fields=["public_identifier"])
        elif not pid and not lead.public_identifier:
            pid = f"_unknown_{lead.pk}"
            logger.debug("Backfill: Lead %d → public_identifier='%s'", lead.pk, pid)
            lead.public_identifier = pid
            lead.save(update_fields=["public_identifier"])

    # Clear stale profile_data on /in/me/ marker so self_profile re-fetches with urn.
    Lead.objects.filter(linkedin_url="https://www.linkedin.com/in/me/").update(profile_data=None)

    for lead in Lead.objects.filter(linkedin_url=""):
        url = _public_id_to_url(lead.public_identifier)
        logger.debug("Backfill: Lead %d linkedin_url='' → '%s'", lead.pk, url)
        lead.linkedin_url = url
        lead.save(update_fields=["linkedin_url"])


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0002_rename_description_to_profile_data"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="lead",
            name="public_identifier",
            field=models.CharField(max_length=200, unique=True),
        ),
        migrations.AlterField(
            model_name="lead",
            name="linkedin_url",
            field=models.URLField(max_length=200, unique=True),
        ),
    ]
