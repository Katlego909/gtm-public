from django.db import models
from django.contrib.auth.models import User
from gtm.models import AssessmentSession
from gtm.models_workspace import Workspace
import uuid

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
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='gap_metrics', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='gap_metrics', null=True, blank=True)

    class Meta:
        unique_together = ('metric', 'session', 'workspace')

    def __str__(self):
        return self.metric

    @property
    def metric_field_name(self):
        return self.METRIC_FIELD_MAPPING.get(self.metric)


class GapAnalysisSuggestion(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Review'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
    ]

    category = models.CharField(max_length=100, choices=GapAnalysisMetric.CATEGORY_CHOICES)
    metric = models.CharField(max_length=100, choices=GapAnalysisMetric.METRIC_CHOICES)
    current = models.FloatField(default=0)
    target = models.FloatField()
    priority = models.CharField(max_length=10, choices=GapAnalysisMetric.PRIORITY_CHOICES)
    recommendation = models.TextField()
    rationale = models.TextField(blank=True, default='')
    confidence = models.PositiveSmallIntegerField(default=70)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')

    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.CASCADE,
        related_name='gap_suggestions',
        null=True,
        blank=True,
    )
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='gap_suggestions',
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='gap_suggestions',
    )
    source_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['user', 'status']),
        ]

    def __str__(self):
        return f"{self.metric} ({self.status})"

class Resource(models.Model):
    """
    A shared document or link within a workspace.
    Centralizes GTM assets like Sales Decks, Brand Guidelines, and CRM links.
    """
    RESOURCE_TYPES = [
        ('file', 'File Upload'),
        ('link', 'External Link'),
    ]
    
    CATEGORIES = [
        ('strategy', 'Strategy & Planning'),
        ('sales', 'Sales Enablement'),
        ('marketing', 'Marketing Assets'),
        ('product', 'Product & Tech'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="resources")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    resource_type = models.CharField(max_length=10, choices=RESOURCE_TYPES, default='link')
    category = models.CharField(max_length=20, choices=CATEGORIES, default='other')
    
    # For 'file' type
    file = models.FileField(upload_to='workspace_resources/%Y/%m/', null=True, blank=True)
    
    # For 'link' type
    url = models.URLField(max_length=500, null=True, blank=True)
    
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="uploaded_resources")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"

    @property
    def extension(self):
        if self.resource_type == 'link':
            return 'link'
        if self.file:
            import os
            _, ext = os.path.splitext(self.file.name)
            return ext.lower().replace('.', '')
        return 'unknown'

class AIResourceRecommendation(models.Model):
    """
    Links a GTM AssessmentSession to a recommended Resource from the workspace library,
    based on AI analyzing the Assessment's weakest categories.
    """
    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE, related_name="ai_resource_matches")
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="ai_recommendations")
    rationale = models.CharField(max_length=255, help_text="Short explanation of why this resource was recommended by AI")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('session', 'resource')
        ordering = ['-created_at']

    def __str__(self):
        return f"Recommendation: {self.resource.name} for {self.session.uuid}"
