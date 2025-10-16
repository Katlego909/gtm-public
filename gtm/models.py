from django.db import models
import uuid

class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    weight = models.FloatField(default=1.0)  # Demand 0.4, Conversion 0.4, Delivery 0.2 (normalized later)
    def __str__(self): return self.name

class Question(models.Model):
    id_code = models.CharField(max_length=10, unique=True)  # e.g., D1, C3
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    weight = models.FloatField(default=1.0)  # per-question weight (e.g., 1.2)
    diagnostic_note = models.CharField(max_length=200, blank=True)
    def __str__(self): return f"{self.id_code} – {self.text[:60]}"

class AssessmentSession(models.Model):
    """A single run of the health check."""
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    company_name = models.CharField(max_length=120, blank=True)
    industry = models.CharField(max_length=120, blank=True)
    current_step = models.PositiveIntegerField(default=1)
    is_completed = models.BooleanField(default=False)
    
    owner_client_id = models.CharField(max_length=64, db_index=True, blank=True, default="")

class Response(models.Model):
    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE, related_name="responses")
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    score = models.PositiveSmallIntegerField()  # 1..5

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
    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE, related_name="actions")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, null=True, blank=True)
    note = models.CharField(max_length=240)
    owner = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="todo")
    due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
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
