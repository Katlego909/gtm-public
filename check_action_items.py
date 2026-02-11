import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
django.setup()

from gtm.models import ActionItem

print('Total ActionItems:', ActionItem.objects.count())
print('Todo:', ActionItem.objects.filter(status='todo').count())
print('Doing:', ActionItem.objects.filter(status='doing').count())
print('Done:', ActionItem.objects.filter(status='done').count())
print('\nSample items:')
for item in ActionItem.objects.all()[:5]:
    print(f'  - {item.note[:50]} (status: {item.status})')
