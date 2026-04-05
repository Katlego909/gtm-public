"""
Backfill created_by on ActionItem and user on GapAnalysisMetric
for existing items that don't have an owner set.
"""
from django.core.management.base import BaseCommand
from gtm.models import ActionItem
from dashboard.models import GapAnalysisMetric


class Command(BaseCommand):
    help = 'Backfill ownership fields on existing items'

    def handle(self, *args, **options):
        # Backfill ActionItem.created_by from session.user
        items = ActionItem.objects.filter(created_by__isnull=True, session__isnull=False)
        count = 0
        for item in items.select_related('session__user'):
            if item.session and item.session.user:
                item.created_by = item.session.user
                item.save(update_fields=['created_by'])
                count += 1
        self.stdout.write(self.style.SUCCESS(f'Backfilled {count} action items'))

        # Backfill GapAnalysisMetric.user from session.user  
        metrics = GapAnalysisMetric.objects.filter(user__isnull=True, session__isnull=False)
        count = 0
        for metric in metrics.select_related('session__user'):
            if metric.session and metric.session.user:
                metric.user = metric.session.user
                metric.save(update_fields=['user'])
                count += 1
        self.stdout.write(self.style.SUCCESS(f'Backfilled {count} gap metrics'))
