from django.db import models
import uuid
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

# Import workspace models so Django can find them
from .models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation

# AI generation status choices
AI_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("generating", "Generating"),
    ("done", "Done"),
    ("failed", "Failed"),
]

class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    weight = models.FloatField(default=1.0)  # Demand 0.4, Conversion 0.4, Delivery 0.2 (normalized later)
    
    class Meta:
        verbose_name_plural = "Categories"
       
    
    def __str__(self): return self.name

class Question(models.Model):
    id_code = models.CharField(max_length=10, unique=True)  # e.g., D1, C3
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    weight = models.FloatField(default=1.0)  # per-question weight (e.g., 1.2)
    diagnostic_note = models.CharField(max_length=200, blank=True)
    ai_metadata = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    input_type = models.CharField(max_length=50, blank=True, default="scale")
    input_options = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    input_option_labels = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    def __str__(self): return f"{self.id_code} – {self.text[:60]}"

COMPANY_STAGE_CHOICES = [
    ("idea", "Idea / Pre-launch"),
    ("pre_revenue", "Early Stage / Pre-Revenue"),
    ("early_revenue", "Early Revenue"),
    ("growth", "Growth"),
    ("scale", "Scale / Mature"),
]

class AssessmentSession(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_client_id = models.CharField(max_length=64, db_index=True)
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="gtm_sessions"
    )
    
    # Workspace relationship for collaboration
    workspace = models.ForeignKey(
        'Workspace',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="assessments",
        help_text="Workspace this assessment belongs to"
    )

    # Existing
    company_name = models.CharField(max_length=120, blank=True, default="")
    industry = models.CharField(max_length=120, blank=True, default="")

    # New firmographic + contact fields
    website = models.URLField(blank=True, default="")
    contact_name = models.CharField(max_length=120, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_role = models.CharField(max_length=120, blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")

    company_size = models.CharField(  # e.g. “1–10”, “11–50”, etc.
        max_length=20, blank=True, default=""
    )
    revenue_range = models.CharField(  # e.g. “<$1M”, “$1–10M”, “$10–50M”, “>$50M”
        max_length=20, blank=True, default=""
    )
    country = models.CharField(max_length=80, blank=True, default="")
    crm = models.CharField(max_length=80, blank=True, default="")  # HubSpot, SFDC, etc.

    # Company lifecycle stage. Blank = unset/legacy → treated as "all pillars
    # apply" (today's behavior). Drives which assessment pillars are shown
    # and scored — see STAGE_CATEGORY_APPLICABILITY in services.py.
    company_stage = models.CharField(max_length=20, choices=COMPANY_STAGE_CHOICES, blank=True, default="")

    # Acquisition / attribution
    utm_source = models.CharField(max_length=80, blank=True, default="")
    utm_medium = models.CharField(max_length=80, blank=True, default="")
    utm_campaign = models.CharField(max_length=120, blank=True, default="")
    referrer = models.CharField(max_length=200, blank=True, default="")

    # Misc
    notes = models.TextField(blank=True, default="")

    is_completed = models.BooleanField(default=False)
    current_step = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        label = self.company_name or "Untitled company"
        return f"{label} • {self.uuid}"

class Response(models.Model):
    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE, related_name="responses")
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    score = models.PositiveSmallIntegerField()  # 1..5

    ai_insight = models.TextField(blank=True, default="")
    ai_insight_status = models.CharField(max_length=12, choices=AI_STATUS_CHOICES, default="pending", db_index=True)
    context_note = models.TextField(blank=True, default="")  # User-provided business context

    class Meta:
        unique_together = ("session", "question")

class RecommendationBand(models.Model):
    """Score bands → stage + recommended actions."""
    min_score = models.FloatField()   # inclusive
    max_score = models.FloatField()   # inclusive
    stage = models.CharField(max_length=40)
    headline = models.CharField(max_length=120)
    actions_markdown = models.TextField()

    class Meta:
        ordering = ["min_score"]

    def __str__(self): return f"{self.stage} ({self.min_score}-{self.max_score})"

class ActionItem(models.Model):
    STATUS_CHOICES = [("todo","To do"),("doing","In progress"),("done","Done")]
    
    id = models.AutoField(primary_key=True)
    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE, related_name="actions", null=True, blank=True)
    question = models.ForeignKey(Question, on_delete=models.CASCADE, null=True, blank=True)
    
    # Workspace and team collaboration
    workspace = models.ForeignKey(
        'Workspace',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="action_items",
        help_text="Workspace this action item belongs to"
    )
    
    note = models.CharField(max_length=240)
    owner = models.CharField(max_length=120, blank=True)  # Legacy field
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="created_action_items"
    )
    
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="assigned_action_items"
    )
    
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="todo")
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    deliverable_document = models.ForeignKey(
        'AgentDocument',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="completed_action_items",
        help_text="The concrete artifact an AI agent produced to complete this task, if any.",
    )

    def __str__(self):
        return self.note[:50]

class ActionItemComment(models.Model):
    """
    Comments left by team members on a specific Action Item.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action_item = models.ForeignKey(ActionItem, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="task_comments")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    duration_ms = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Wall-clock time for the AI completion turn that produced this comment, if any."
    )
    prompt_token_count = models.PositiveIntegerField(null=True, blank=True)
    candidates_token_count = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Comment by {self.user.username} on task {self.action_item.id}"

class ToolRecommendation(models.Model):
    category = models.ForeignKey("Category", on_delete=models.CASCADE)
    keyword = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    tools = models.TextField(help_text="Comma-separated list of tools")
    url = models.URLField(blank=True, null=True)

    def tool_list(self):
        return [t.strip() for t in self.tools.split(",") if t.strip()]

    def __str__(self):
        return f"{self.keyword} ({self.category.name})"   

class ResultSnapshot(models.Model):
    session = models.OneToOneField(
        "AssessmentSession", on_delete=models.CASCADE, related_name="snapshot"
    )
    overall = models.FloatField()
    band = models.ForeignKey(
        "RecommendationBand", on_delete=models.SET_NULL, null=True, blank=True
    )
    band_stage = models.CharField(max_length=40, blank=True)   
    band_headline = models.CharField(max_length=200, blank=True)
    category_breakdown = models.JSONField(encoder=DjangoJSONEncoder)
    radar_labels = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    radar_values = models.JSONField(encoder=DjangoJSONEncoder, default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    company_name   = models.CharField(max_length=120, blank=True, default="")
    industry       = models.CharField(max_length=120, blank=True, default="")
    website        = models.URLField(blank=True, default="")
    contact_name   = models.CharField(max_length=120, blank=True, default="")
    contact_email  = models.EmailField(blank=True, default="")
    contact_role   = models.CharField(max_length=120, blank=True, default="")
    phone          = models.CharField(max_length=40,  blank=True, default="")
    company_size   = models.CharField(max_length=20,  blank=True, default="")
    revenue_range  = models.CharField(max_length=20,  blank=True, default="")
    country        = models.CharField(max_length=80,  blank=True, default="")
    crm            = models.CharField(max_length=80,  blank=True, default="")
    company_stage  = models.CharField(max_length=20,  blank=True, default="")
    utm_source     = models.CharField(max_length=80,  blank=True, default="")
    utm_medium     = models.CharField(max_length=80,  blank=True, default="")
    utm_campaign   = models.CharField(max_length=120, blank=True, default="")
    referrer       = models.CharField(max_length=200, blank=True, default="")
    report_sent = models.BooleanField(default=False)
    ai_playbook = models.TextField(blank=True, default="")
    ai_playbook_status = models.CharField(max_length=12, choices=AI_STATUS_CHOICES, default="pending", db_index=True)
    ai_risk_status = models.CharField(max_length=20, blank=True, default="")
    ai_financial_summary = models.TextField(blank=True, default="")
    ai_competitor_analysis = models.TextField(blank=True, default="")

    def __str__(self):
        return f"Snapshot for {self.session.uuid} – {self.overall}/100"


class AIScoringExample(models.Model):
    """A real Gemini scoring outcome, captured for reuse as a fallback signal
    when Gemini is unavailable (quota/network/credentials failure).

    Deliberately stores only an embedding of the evidence passage plus the
    score/confidence Gemini assigned to it — never the raw evidence text,
    Gemini's reasoning text, or anything identifying the company/session —
    so this corpus can be safely shared and queried across companies without
    leaking one company's document content into another's fallback results.
    """
    question_id_code = models.CharField(max_length=10, db_index=True)
    evidence_embedding = models.JSONField(encoder=DjangoJSONEncoder)  # 384-dim MiniLM vector
    score = models.PositiveSmallIntegerField()
    confidence = models.CharField(max_length=10, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.question_id_code}: {self.score} ({self.confidence or 'n/a'})"


class ChatMessage(models.Model):
    """Store chat conversation history for AI assistant"""
    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.CASCADE,
        related_name="chat_messages"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    message = models.TextField()  
    response = models.TextField()  
    attachments = models.JSONField(default=list, blank=True)
    intent = models.CharField(max_length=50, blank=True)  
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Chat {self.session.uuid} at {self.created_at}"


class WorkspaceChatMessage(models.Model):
    """Chat history for the workspace-scoped agents (portfolio, resource,
    insights). Kept separate from ChatMessage, which is hard-tied to a
    single AssessmentSession."""

    AGENT_TYPE_CHOICES = [
        ("portfolio", "Portfolio Agent"),
        ("resource", "Resource Agent"),
        ("insights", "Insights Agent"),
    ]

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="agent_chat_messages",
    )
    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_agent_chat_messages",
    )
    agent_type = models.CharField(max_length=20, choices=AGENT_TYPE_CHOICES, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    message = models.TextField()
    response = models.TextField()
    attachments = models.JSONField(default=list, blank=True)
    intent = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['workspace', 'agent_type', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_agent_type_display()} chat in workspace {self.workspace_id} at {self.created_at}"


class AgentDocument(models.Model):
    """A document created and editable by any of the 4 GTM agents. Content
    is stored as markdown text (never a FileField -- this app's default
    storage is local filesystem, which doesn't persist across Cloud Run
    instances/restarts) and rendered to PDF/Word/Markdown on export."""

    DOC_TYPE_CHOICES = [
        ("client_summary", "Client Summary"),
        ("action_plan", "Action Plan"),
        ("roadmap", "Roadmap"),
        ("resource_brief", "Resource Brief"),
        ("action_item_deliverable", "Action Item Deliverable"),
        ("other", "Other"),
    ]
    AGENT_TYPE_CHOICES = [
        ("gtm_strategist", "GTM Strategist"),
        ("portfolio", "Portfolio Agent"),
        ("resource", "Resource Agent"),
        ("insights", "Insights Agent"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="agent_documents",
    )
    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="agent_documents",
    )
    agent_type = models.CharField(max_length=20, choices=AGENT_TYPE_CHOICES)
    doc_type = models.CharField(max_length=30, choices=DOC_TYPE_CHOICES, default="other")
    title = models.CharField(max_length=200)
    content = models.TextField()
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.title} (v{self.version})"


class GTMFile(models.Model):
    """
    Strategic Evidence uploaded by the user to be audited by the GTM Agent.
    Supports Image (Landing Pages/Ads) and PDF (Sales Decks/Strategy).
    Can be attached to an AssessmentSession directly or uploaded to a Workspace
    via the Strategic Asset Library.
    """
    FILE_TYPES = [
        ('landing_page', 'Landing Page Screenshot'),
        ('ad_creative', 'Ad Creative Asset'),
        ('sales_deck', 'Sales Deck / Strategy PDF'),
        ('other', 'Other Strategic Evidence'),
    ]

    AUDIT_STATUS_CHOICES = [
        ('none', 'Not Audited'),
        ('pending', 'Awaiting Audit'),
        ('auditing', 'Auditing…'),
        ('complete', 'Audit Complete'),
        ('failed', 'Audit Failed'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AssessmentSession, 
        on_delete=models.CASCADE, 
        related_name="evidence_files",
        null=True, blank=True,
    )
    workspace = models.ForeignKey(
        'Workspace',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="gtm_files",
        help_text="Optional: workspace-level evidence (used by the Asset Library)"
    )
    
    file = models.FileField(
        upload_to="gtm/evidence/%Y/%m/%d/",
        max_length=255
    )
    file_type = models.CharField(
        max_length=40, 
        choices=FILE_TYPES, 
        default='other'
    )
    audit_status = models.CharField(
        max_length=10, choices=AUDIT_STATUS_CHOICES, default='none'
    )
    
    # AI STRATEGIC AUDIT
    ai_audit_notes = models.TextField(
        blank=True, 
        default="",
        help_text="The AI Auditor's critique of this visual/document asset."
    )
    ai_audit_score_modifier = models.IntegerField(
        default=0,
        help_text="Suggested impact on the GTM score (-5 to +5) based on evidence."
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "GTM Strategic Evidence"
        verbose_name_plural = "GTM Strategic Evidence"

    def __str__(self):
        return f"{self.get_file_type_display()} - {self.session_id or self.workspace_id}"


class DeliveryDocument(models.Model):
    """
    Documents uploaded by a user to auto-score the Delivery section via AI analysis.
    The AI reads these files, extracts evidence, and proposes a 1-5 score per question.
    Humans review AI scores and can override any of them before submitting.
    """

    ANALYSIS_STATUS = [
        ('uploaded', 'Uploaded'),
        ('analyzing', 'Analyzing'),
        ('complete', 'Analysis Complete'),
        ('failed', 'Analysis Failed'),
    ]

    FILE_TYPE_CHOICES = [
        ('csv', 'CSV Spreadsheet'),
        ('xlsx', 'Excel Spreadsheet'),
        ('pdf', 'PDF Document'),
        ('docx', 'Word Document'),
        ('txt', 'Text / Notepad File'),
        ('json', 'JSON Data'),
        ('image', 'Image / Screenshot'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AssessmentSession, on_delete=models.CASCADE, related_name='delivery_docs'
    )
    file = models.FileField(upload_to='gtm/delivery/%Y/%m/%d/', max_length=255)
    original_filename = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    file_type = models.CharField(max_length=10, choices=FILE_TYPE_CHOICES, default='other')
    extracted_text = models.TextField(blank=True, default='')
    analysis_result = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    analysis_status = models.CharField(max_length=12, choices=ANALYSIS_STATUS, default='uploaded')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Delivery Evidence Document"
        verbose_name_plural = "Delivery Evidence Documents"

    def __str__(self):
        return f"{self.original_filename} ({self.session_id})"


class CategoryDocument(models.Model):
    """
    Documents uploaded to auto-score Demand or Conversion sections via AI analysis.
    Mirrors DeliveryDocument but is shared across non-delivery categories.
    """

    CATEGORY_CHOICES = [
        ('demand', 'Demand'),
        ('conversion', 'Conversion'),
    ]

    ANALYSIS_STATUS = [
        ('uploaded', 'Uploaded'),
        ('analyzing', 'Analyzing'),
        ('complete', 'Analysis Complete'),
        ('failed', 'Analysis Failed'),
    ]

    FILE_TYPE_CHOICES = [
        ('csv', 'CSV Spreadsheet'),
        ('xlsx', 'Excel Spreadsheet'),
        ('pdf', 'PDF Document'),
        ('docx', 'Word Document'),
        ('txt', 'Text / Notepad File'),
        ('json', 'JSON Data'),
        ('image', 'Image / Screenshot'),
        ('other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AssessmentSession, on_delete=models.CASCADE, related_name='category_docs'
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    file = models.FileField(upload_to='gtm/category/%Y/%m/%d/', max_length=255)
    original_filename = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    file_type = models.CharField(max_length=10, choices=FILE_TYPE_CHOICES, default='other')
    extracted_text = models.TextField(blank=True, default='')
    analysis_result = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    analysis_status = models.CharField(max_length=12, choices=ANALYSIS_STATUS, default='uploaded')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Category Evidence Document"
        verbose_name_plural = "Category Evidence Documents"

    def __str__(self):
        return f"[{self.category}] {self.original_filename} ({self.session_id})"
