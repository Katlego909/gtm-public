from django.core.management.base import BaseCommand
from dashboard.models import GapAnalysisMetric

class Command(BaseCommand):
    help = 'Loads initial data for Gap Analysis Metrics'

    def handle(self, *args, **kwargs):
        metrics_data = [
            {
                'category': 'Lead Generation',
                'metric': 'Monthly Qualified Leads',
                'target': 800,
                'priority': 'High',
                'recommendation': 'Increase content marketing budget by 40% and launch ABM campaigns',
            },
            {
                'category': 'Sales Efficiency',
                'metric': 'Average Deal Size',
                'target': 35000,
                'priority': 'Medium',
                'recommendation': 'Focus on enterprise segment and implement value-based selling training',
            },
            {
                'category': 'Customer Success',
                'metric': 'Net Revenue Retention',
                'target': 120,
                'priority': 'Low',
                'recommendation': 'Launch upsell playbook and quarterly business reviews program',
            },
            {
                'category': 'Product Marketing',
                'metric': 'Product Qualified Leads',
                'target': 600,
                'priority': 'High',
                'recommendation': 'Improve free trial onboarding and add in-app engagement triggers',
            },
            {
                'category': 'Sales Velocity',
                'metric': 'Win Rate',
                'target': 28,
                'priority': 'High',
                'recommendation': 'Implement better lead qualification and competitive battle cards',
            },
            {
                'category': 'Marketing ROI',
                'metric': 'CAC Payback Period',
                'target': 9,
                'priority': 'Medium',
                'recommendation': 'Optimize high-performing channels and reduce spend on underperformers',
            },
        ]

        for data in metrics_data:
            GapAnalysisMetric.objects.get_or_create(metric=data['metric'], defaults=data)

        self.stdout.write(self.style.SUCCESS('Successfully loaded Gap Analysis Metrics.'))
