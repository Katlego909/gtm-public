from django.db import migrations


def backfill_workspace_event_session(apps, schema_editor):
    WorkspaceActivityEvent = apps.get_model('gtm', 'WorkspaceActivityEvent')
    ActionItem = apps.get_model('gtm', 'ActionItem')

    events = WorkspaceActivityEvent.objects.filter(session__isnull=True, object_type='action_item')
    for event in events.iterator(chunk_size=200):
        object_id = (event.object_id or '').strip()
        if not object_id:
            continue

        try:
            action_item_id = int(object_id)
        except (TypeError, ValueError):
            continue

        action_item = ActionItem.objects.filter(id=action_item_id).first()
        if not action_item:
            # Orphan activity row left behind after task/session deletion.
            event.delete()
            continue

        if action_item.session_id:
            event.session_id = action_item.session_id
            event.save(update_fields=['session'])


class Migration(migrations.Migration):

    dependencies = [
        ('gtm', '0023_workspaceactivityevent_session'),
    ]

    operations = [
        migrations.RunPython(backfill_workspace_event_session, migrations.RunPython.noop),
    ]
