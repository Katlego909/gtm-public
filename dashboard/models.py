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
    # Maps each gap category to the assessment pillar (Category.name in the
    # gtm app) whose deterministic score movement is the honest progress
    # signal for gaps in that category.
    CATEGORY_PILLAR_MAPPING = {
        'Lead Generation': 'Demand',
        'Marketing ROI': 'Demand',
        'Product Marketing': 'Demand',
        'Sales Efficiency': 'Conversion',
        'Sales Velocity': 'Conversion',
        'Customer Success': 'Delivery',
    }
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('closed', 'Closed'),
    ]
    CLOSED_REASON_CHOICES = [
        ('target_reached', 'Target reached'),
        ('manual', 'Manually resolved'),
    ]
    ESTIMATE_METHOD_CHOICES = [
        ('gemini', 'AI Estimate'),
        ('formula', 'Formula Estimate'),
        ('manual_entry', 'Manual Entry'),
        ('measured', 'Measured'),
    ]
    # How precise a number for this metric is honest to display -- current/
    # target get rounded to the nearest multiple of this, regardless of what
    # the AI (or the fallback formula) returns, so a rough estimate never
    # implies false precision (e.g. "96.3 leads").
    METRIC_VALUE_GRANULARITY = {
        'Monthly Qualified Leads': 1,
        'Product Qualified Leads': 1,
        'Win Rate': 1,
        'Net Revenue Retention': 1,
        'Average Deal Size': 100,
        'CAC Payback Period': 0.5,
    }

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

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='open')
    closed_reason = models.CharField(max_length=20, choices=CLOSED_REASON_CHOICES, blank=True, default='')
    closed_at = models.DateTimeField(null=True, blank=True)
    estimate_method = models.CharField(
        max_length=20, choices=ESTIMATE_METHOD_CHOICES, blank=True, default='',
        help_text="Where the current value actually came from -- an AI guess, a deterministic "
                   "fallback formula, a manually typed number, or a real measured check-in.",
    )

    class Meta:
        unique_together = ('metric', 'session', 'workspace')

    def __str__(self):
        return self.metric

    @property
    def metric_field_name(self):
        return self.METRIC_FIELD_MAPPING.get(self.metric)

    @property
    def pillar_name(self):
        return self.CATEGORY_PILLAR_MAPPING.get(self.category)

    @classmethod
    def round_metric_value(cls, metric_name, value):
        """Round a current/target value to the precision that's honest for
        this metric, instead of showing spurious decimal precision from an
        AI guess or a formula."""
        step = cls.METRIC_VALUE_GRANULARITY.get(metric_name, 1)
        return round(round(value / step) * step, 2)


class GapMetricMeasurement(models.Model):
    """One real, dated data point for a gap metric's current value.

    Only genuinely measured values belong here (a human check-in today,
    a CRM pull later) -- AI estimates never create measurements, so this
    history is what makes 'gap closed' an honest claim.
    """

    SOURCE_CHOICES = [
        ('manual', 'Manual check-in'),
        ('crm', 'CRM'),
    ]

    gap_metric = models.ForeignKey(GapAnalysisMetric, on_delete=models.CASCADE, related_name='measurements')
    value = models.FloatField()
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')
    note = models.CharField(max_length=240, blank=True, default='')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='gap_measurements')
    measured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-measured_at']

    def __str__(self):
        return f"{self.gap_metric.metric}: {self.value} ({self.measured_at:%Y-%m-%d})"


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

    AUDIT_STATUS_CHOICES = [
        ('none', 'Not Audited'),
        ('pending', 'Awaiting Audit'),
        ('auditing', 'Auditing…'),
        ('complete', 'Audit Complete'),
        ('failed', 'Audit Failed'),
        ('no_credits', 'AI Credits Exhausted'),
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

    # AI STRATEGIC AUDIT fields
    audit_status = models.CharField(max_length=10, choices=AUDIT_STATUS_CHOICES, default='none')
    ai_audit_summary = models.TextField(blank=True, default="")
    ai_score_modifier = models.IntegerField(
        default=0,
        help_text="Advisory score impact (−5 to +5) suggested by the AI auditor."
    )
    ai_audit_at = models.DateTimeField(null=True, blank=True)
    gtm_categories = models.JSONField(
        default=list, blank=True,
        help_text="List of GTM categories this asset most impacts."
    )

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

    @property
    def is_image(self):
        return self.resource_type == 'file' and self.extension in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp')

    @property
    def ai_audit_summary_html(self):
        """Convert markdown audit summary to HTML for display."""
        if not self.ai_audit_summary:
            return ""
        try:
            import markdown
            html = markdown.markdown(self.ai_audit_summary, extensions=['extra', 'codehilite'])
            return html
        except ImportError:
            # Fallback if markdown not available - just use the raw text with line breaks
            import html as html_module
            escaped = html_module.escape(self.ai_audit_summary)
            return escaped.replace('\n', '<br>')

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


class Notification(models.Model):
    """
    User-specific actionable alerts for tasks, invites, and AI insights.
    Unlike ActivityEvents, these are meant to be 'cleared' by the user.
    """
    LEVEL_CHOICES = [
        ('success', 'Success'),
        ('info', 'Information'),
        ('warning', 'Warning'),
        ('danger', 'Urgent'),
    ]
    
    TYPE_CHOICES = [
        ('task', 'Task Assignment'),
        ('task_status', 'Task Status Update'),
        ('invite', 'Workspace Invitation'),
        ('ai_report', 'AI Report Ready'),
        ('system', 'System Message'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_notifications")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications")
    
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='info')
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='system')
    
    title = models.CharField(max_length=120)
    message = models.TextField()
    link = models.CharField(max_length=255, blank=True, default="")
    
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
            models.Index(fields=['workspace', 'is_read']),
        ]

    def __str__(self):
        return f"{self.title} for {self.recipient.username}"


class UserSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='settings')

    # Email notifications
    email_task_assigned = models.BooleanField(default=True)
    email_task_completed = models.BooleanField(default=True)
    email_workspace_invite = models.BooleanField(default=True)
    email_ai_insights = models.BooleanField(default=False)

    # In-app notifications
    inapp_task_assigned = models.BooleanField(default=True)
    inapp_task_completed = models.BooleanField(default=True)
    inapp_workspace_activity = models.BooleanField(default=True)
    inapp_ai_insights = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'User Settings'

    def __str__(self):
        return f"Settings for {self.user.username}"
