"""Fold ``llm_provider`` into ``ai_model`` as a pydantic-ai ``provider:model`` id.

The separate provider field could drift out of sync with the model/key (it
defaulted to ``openai`` while the model/key were Anthropic, sending an sk-ant-
key to OpenAI's endpoint). Collapsing the provider into the model string makes
that mismatch structurally impossible.

Data migration rule (per existing SiteConfig row):
- already ``provider:model`` → leave it.
- bare model with an unambiguous prefix (claude→anthropic, gpt/o1/o3→openai,
  gemini→google) → trust the prefix, not the stored provider (this is what
  auto-corrects a ``claude-*`` model wrongly parked on provider ``openai``).
- otherwise → fall back to the stored ``llm_provider``.
"""
from django.db import migrations, models

_LEGACY_MODEL_PREFIXES = {
    "gpt": "openai", "o1": "openai", "o3": "openai",
    "claude": "anthropic", "gemini": "google",
}


def fold_provider_into_model(apps, schema_editor):
    SiteConfig = apps.get_model("core", "SiteConfig")
    for cfg in SiteConfig.objects.all():
        model = (cfg.ai_model or "").strip()
        if not model or ":" in model:
            continue
        provider = next(
            (p for prefix, p in _LEGACY_MODEL_PREFIXES.items() if model.startswith(prefix)),
            cfg.llm_provider or "openai",
        )
        cfg.ai_model = f"{provider}:{model}"
        cfg.save(update_fields=["ai_model"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_rename_finder_api_key_bettercontact"),
    ]

    operations = [
        migrations.RunPython(fold_provider_into_model, migrations.RunPython.noop),
        migrations.RemoveField(model_name="siteconfig", name="llm_provider"),
        migrations.AlterField(
            model_name="siteconfig",
            name="ai_model",
            field=models.CharField(
                blank=True,
                default="",
                help_text="provider:model, e.g. anthropic:claude-sonnet-4-5-20250929",
                max_length=200,
            ),
        ),
    ]
