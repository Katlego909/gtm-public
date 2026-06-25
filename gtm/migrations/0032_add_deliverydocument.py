from django.db import migrations, models
import django.core.serializers.json
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('gtm', '0031_add_playbook_enrichment_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeliveryDocument',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('file', models.FileField(max_length=255, upload_to='gtm/delivery/%Y/%m/%d/')),
                ('original_filename', models.CharField(blank=True, max_length=255)),
                ('file_size', models.PositiveIntegerField(default=0)),
                ('file_type', models.CharField(
                    choices=[
                        ('csv', 'CSV Spreadsheet'),
                        ('xlsx', 'Excel Spreadsheet'),
                        ('pdf', 'PDF Document'),
                        ('docx', 'Word Document'),
                        ('txt', 'Text / Notepad File'),
                        ('json', 'JSON Data'),
                        ('image', 'Image / Screenshot'),
                        ('other', 'Other'),
                    ],
                    default='other',
                    max_length=10,
                )),
                ('extracted_text', models.TextField(blank=True, default='')),
                ('analysis_result', models.JSONField(
                    blank=True,
                    default=dict,
                    encoder=django.core.serializers.json.DjangoJSONEncoder,
                )),
                ('analysis_status', models.CharField(
                    choices=[
                        ('uploaded', 'Uploaded'),
                        ('analyzing', 'Analyzing'),
                        ('complete', 'Analysis Complete'),
                        ('failed', 'Analysis Failed'),
                    ],
                    default='uploaded',
                    max_length=12,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='delivery_docs',
                    to='gtm.assessmentsession',
                )),
            ],
            options={
                'verbose_name': 'Delivery Evidence Document',
                'verbose_name_plural': 'Delivery Evidence Documents',
                'ordering': ['-created_at'],
            },
        ),
    ]
