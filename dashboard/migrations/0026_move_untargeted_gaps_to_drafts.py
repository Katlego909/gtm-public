"""Move gaps an agent flagged without a target back to review drafts.

Agents used to write gap rows straight into the live table, often with a
target of 0 and no measurement. Those rows looked "on target" (0.0%) and
would close on the first value logged. Each one that nobody has measured or
attached tasks to becomes a pending suggestion that needs a target before
it can be accepted. Rows with measurements or tasks are left alone.

Reversing recreates the rows from the suggestions this migration made that
are still pending."""

from django.db import migrations


def to_drafts(apps, schema_editor):
    GapAnalysisMetric = apps.get_model("dashboard", "GapAnalysisMetric")
    GapAnalysisSuggestion = apps.get_model("dashboard", "GapAnalysisSuggestion")
    WorkspaceMembership = apps.get_model("gtm", "WorkspaceMembership")

    rows = GapAnalysisMetric.objects.filter(
        source="AI", target__lte=0, measurements__isnull=True, action_items__isnull=True,
    ).distinct()
    for row in rows:
        user = row.user
        if user is None and row.workspace_id:
            admin = WorkspaceMembership.objects.filter(
                workspace_id=row.workspace_id, role="admin", is_active=True,
            ).order_by("id").first()
            user = admin.user if admin else None
        if user is None:
            continue
        GapAnalysisSuggestion.objects.create(
            category=row.category,
            metric=row.metric,
            current=0,
            target=0,
            priority=row.priority,
            recommendation=row.recommendation,
            rationale="Flagged without a target; set one to accept.",
            status="pending",
            session_id=row.session_id,
            workspace_id=row.workspace_id,
            user=user,
            source_payload={
                "generator": "agent", "unmeasured": True, "migrated": True,
                "original": {"user_id": row.user_id, "current": row.current, "estimate_method": row.estimate_method},
            },
        )
        row.delete()


def back_to_rows(apps, schema_editor):
    GapAnalysisMetric = apps.get_model("dashboard", "GapAnalysisMetric")
    GapAnalysisSuggestion = apps.get_model("dashboard", "GapAnalysisSuggestion")

    for draft in GapAnalysisSuggestion.objects.filter(status="pending", source_payload__migrated=True):
        original = draft.source_payload.get("original", {})
        GapAnalysisMetric.objects.get_or_create(
            metric=draft.metric, session_id=draft.session_id, workspace_id=draft.workspace_id,
            defaults={
                "category": draft.category,
                "current": original.get("current", 0),
                "target": 0,
                "priority": draft.priority,
                "recommendation": draft.recommendation,
                "source": "AI",
                "estimate_method": original.get("estimate_method", ""),
                "user_id": original.get("user_id"),
            },
        )
        draft.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0025_gap_unmeasured_choice"),
        ("gtm", "0062_raise_credit_tiers"),
    ]

    operations = [
        migrations.RunPython(to_drafts, back_to_rows),
    ]
