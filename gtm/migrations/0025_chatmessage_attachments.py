from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gtm', '0024_backfill_workspace_activity_session'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatmessage',
            name='attachments',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
