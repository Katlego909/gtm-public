from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('gtm', '0020_alter_actionitem_assigned_to_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkspaceActivityEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('task_created', 'Task created'), ('task_moved', 'Task moved'), ('task_deleted', 'Task deleted'), ('comment_added', 'Comment added'), ('comment_deleted', 'Comment deleted'), ('resource_created', 'Resource created'), ('resource_updated', 'Resource updated'), ('resource_deleted', 'Resource deleted')], max_length=32)),
                ('summary', models.CharField(max_length=255)),
                ('object_type', models.CharField(blank=True, default='', max_length=32)),
                ('object_id', models.CharField(blank=True, default='', max_length=64)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='workspace_activity_events', to=settings.AUTH_USER_MODEL)),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='activity_events', to='gtm.workspace')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='workspaceactivityevent',
            index=models.Index(fields=['workspace', '-created_at'], name='gtm_workspa_workspa_c2695a_idx'),
        ),
        migrations.AddIndex(
            model_name='workspaceactivityevent',
            index=models.Index(fields=['event_type', '-created_at'], name='gtm_workspa_event_t_dac38c_idx'),
        ),
        migrations.AddIndex(
            model_name='workspaceactivityevent',
            index=models.Index(fields=['actor', '-created_at'], name='gtm_workspa_actor_i_1be33f_idx'),
        ),
    ]
