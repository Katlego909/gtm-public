"""Raise every AI-credit tier 3x, now that agent turns charge every
function-calling request instead of only the last one.

Only accounts still on their old preset budget are raised; a budget someone
set by hand in admin is left alone. Reversing restores the old presets the
same way."""

from django.db import migrations, models

OLD = {"free": 250_000, "pro": 2_500_000, "scale": 10_000_000}
NEW = {"free": 750_000, "pro": 7_500_000, "scale": 30_000_000}
OLD_PERSONAL, NEW_PERSONAL = 100_000, 300_000


def _rebudget(apps, before, after, personal_before, personal_after):
    AICreditAccount = apps.get_model("gtm", "AICreditAccount")
    for tier, budget in before.items():
        AICreditAccount.objects.filter(workspace__isnull=False, tier=tier, token_budget=budget).update(
            token_budget=after[tier]
        )
    AICreditAccount.objects.filter(user__isnull=False, token_budget=personal_before).update(token_budget=personal_after)


def raise_presets(apps, schema_editor):
    _rebudget(apps, OLD, NEW, OLD_PERSONAL, NEW_PERSONAL)


def restore_presets(apps, schema_editor):
    _rebudget(apps, NEW, OLD, NEW_PERSONAL, OLD_PERSONAL)


class Migration(migrations.Migration):

    dependencies = [
        ("gtm", "0061_retired_agent_labels"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aicreditaccount",
            name="token_budget",
            field=models.PositiveIntegerField(
                default=750000,
                help_text=(
                    "Total Gemini prompt+output tokens allowed per period. Source of truth for enforcement; "
                    "can be overridden independently of the tier."
                ),
            ),
        ),
        migrations.RunPython(raise_presets, restore_presets),
    ]
