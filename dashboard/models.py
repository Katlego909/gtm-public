from django.db import models
from gtm.models import AssessmentSession

class Channel(models.Model):
	name = models.CharField(max_length=100, unique=True)
	color = models.CharField(max_length=7, default="#3B82F6")  # HEX color for chart

	def __str__(self):
		return self.name

class ChannelAnalytics(models.Model):

	channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="analytics")
	revenue = models.DecimalField(max_digits=10, decimal_places=2)  # Store actual revenue
	change = models.DecimalField(max_digits=10, decimal_places=2)   # percent change or absolute change
	date = models.DateField(auto_now_add=True)

	def __str__(self):
		return f"{self.channel.name} ({self.date}): {self.revenue} revenue"

class GapAnalysisMetric(models.Model):
    SOURCE_CHOICES = [
        ('AI', 'AI'),
        ('USER', 'User'),
    ]
    CATEGORY_CHOICES = [
        ('Lead Generation', 'Lead Generation'),
        ('Sales Efficiency', 'Sales Efficiency'),
        ('Customer Success', 'Customer Success'),
        ('Product Marketing', 'Product Marketing'),
        ('Sales Velocity', 'Sales Velocity'),
        ('Marketing ROI', 'Marketing ROI'),
    ]
    PRIORITY_CHOICES = [
        ('High', 'High'),
        ('Medium', 'Medium'),
        ('Low', 'Low'),
    ]
    METRIC_FIELD_MAPPING = {
        'Monthly Qualified Leads': 'monthly_qualified_leads',
        'Average Deal Size': 'average_deal_size',
        'Net Revenue Retention': 'net_revenue_retention',
        'Product Qualified Leads': 'product_qualified_leads',
        'Win Rate': 'win_rate',
        'CAC Payback Period': 'cac_payback_period',
    }
    METRIC_CHOICES = [(metric_name, metric_name) for metric_name in METRIC_FIELD_MAPPING.keys()]

    category = models.CharField(max_length=100, choices=CATEGORY_CHOICES)
    metric = models.CharField(max_length=100, choices=METRIC_CHOICES)
    current = models.FloatField(default=0)
    target = models.FloatField()
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES)
    recommendation = models.TextField()
    source = models.CharField(max_length=4, choices=SOURCE_CHOICES, default='USER')
    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE, related_name='gap_metrics', null=True, blank=True)

    class Meta:
        unique_together = ('metric', 'session')

    def __str__(self):
        return self.metric

    @property
    def metric_field_name(self):
        return self.METRIC_FIELD_MAPPING.get(self.metric)
