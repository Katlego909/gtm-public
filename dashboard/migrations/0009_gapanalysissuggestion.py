from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('gtm', '0021_workspaceactivityevent'),
        ('dashboard', '0008_resource'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GapAnalysisSuggestion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category', models.CharField(choices=[('Lead Generation', 'Lead Generation'), ('Sales Efficiency', 'Sales Efficiency'), ('Customer Success', 'Customer Success'), ('Product Marketing', 'Product Marketing'), ('Sales Velocity', 'Sales Velocity'), ('Marketing ROI', 'Marketing ROI')], max_length=100)),
                ('metric', models.CharField(choices=[('Monthly Qualified Leads', 'Monthly Qualified Leads'), ('Average Deal Size', 'Average Deal Size'), ('Net Revenue Retention', 'Net Revenue Retention'), ('Product Qualified Leads', 'Product Qualified Leads'), ('Win Rate', 'Win Rate'), ('CAC Payback Period', 'CAC Payback Period')], max_length=100)),
                ('current', models.FloatField(default=0)),
                ('target', models.FloatField()),
                ('priority', models.CharField(choices=[('High', 'High'), ('Medium', 'Medium'), ('Low', 'Low')], max_length=10)),
                ('recommendation', models.TextField()),
                ('rationale', models.TextField(blank=True, default='')),
                ('confidence', models.PositiveSmallIntegerField(default=70)),
                ('status', models.CharField(choices=[('pending', 'Pending Review'), ('accepted', 'Accepted'), ('rejected', 'Rejected')], default='pending', max_length=10)),
                ('source_payload', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('session', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='gap_suggestions', to='gtm.assessmentsession')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gap_suggestions', to=settings.AUTH_USER_MODEL)),
                ('workspace', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='gap_suggestions', to='gtm.workspace')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='gapanalysissuggestion',
            index=models.Index(fields=['status', '-created_at'], name='dashboard_g_status_aabaad_idx'),
        ),
        migrations.AddIndex(
            model_name='gapanalysissuggestion',
            index=models.Index(fields=['workspace', 'status'], name='dashboard_g_workspa_2f77dd_idx'),
        ),
        migrations.AddIndex(
            model_name='gapanalysissuggestion',
            index=models.Index(fields=['user', 'status'], name='dashboard_g_user_id_f0ef62_idx'),
        ),
    ]
