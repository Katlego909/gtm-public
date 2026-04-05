from django.db import migrations


def dedupe_gap_metrics(apps, schema_editor):
    GapAnalysisMetric = apps.get_model('dashboard', 'GapAnalysisMetric')

    # Workspace-scoped metrics: keep one latest row per (workspace, metric).
    workspace_rows = GapAnalysisMetric.objects.exclude(workspace_id__isnull=True)
    workspace_keys = workspace_rows.values_list('workspace_id', 'metric').distinct()
    for workspace_id, metric in workspace_keys:
        scoped = GapAnalysisMetric.objects.filter(workspace_id=workspace_id, metric=metric).order_by('-id')
        keep = scoped.first()
        if not keep:
            continue
        scoped.exclude(id=keep.id).delete()

    # Personal metrics: keep one latest row per (user, metric) where workspace is null.
    personal_rows = GapAnalysisMetric.objects.filter(workspace_id__isnull=True).exclude(user_id__isnull=True)
    personal_keys = personal_rows.values_list('user_id', 'metric').distinct()
    for user_id, metric in personal_keys:
        scoped = GapAnalysisMetric.objects.filter(
            workspace_id__isnull=True,
            user_id=user_id,
            metric=metric,
        ).order_by('-id')
        keep = scoped.first()
        if not keep:
            continue
        scoped.exclude(id=keep.id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0009_gapanalysissuggestion'),
    ]

    operations = [
        migrations.RunPython(dedupe_gap_metrics, migrations.RunPython.noop),
    ]
