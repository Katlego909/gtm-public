from django.db import migrations
from django.db.models import F


def backfill_completed_at(apps, schema_editor):
    """ActionItem.save() now stamps completed_at whenever a task is done and
    it's missing. Older done tasks reached Done through paths that never set
    it, so without this backfill the next unrelated save would stamp them as
    completed "now" and inflate done-this-week counts. updated_at is the
    best available approximation of when they were finished (it's what the
    dashboard's done-this-week stat used until now)."""
    ActionItem = apps.get_model('gtm', 'ActionItem')
    ActionItem.objects.filter(status='done', completed_at__isnull=True).update(completed_at=F('updated_at'))
    # A task that isn't done can't have a completion time.
    ActionItem.objects.exclude(status='done').filter(completed_at__isnull=False).update(completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('gtm', '0054_workspace_api_key'),
    ]

    operations = [
        migrations.RunPython(backfill_completed_at, migrations.RunPython.noop),
    ]
