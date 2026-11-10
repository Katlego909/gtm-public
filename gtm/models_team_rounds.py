"""
Team rounds: several workspace members answer the same assessment privately,
then the round closes into one consensus AssessmentSession.

Respondents' answers are TeamRoundAnswer rows, not AssessmentSessions, so
they never show up in the many "latest completed assessment" queries across
the app. Only the consensus session does, and it is an ordinary completed
assessment: scoring, AI insights, gaps and the dashboard work on it unchanged.
See gtm/team_rounds.py for the merge rules.
"""
from django.conf import settings
from django.db import models


class TeamRound(models.Model):
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]

    workspace = models.ForeignKey("Workspace", on_delete=models.CASCADE, related_name="team_rounds")
    title = models.CharField(max_length=120)
    # Copied from the workspace's latest assessment when the round opens; picks
    # which pillars are asked (see services._applicable_categories).
    company_stage = models.CharField(max_length=20, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open", db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    consensus_session = models.OneToOneField(
        "AssessmentSession", on_delete=models.SET_NULL, null=True, blank=True, related_name="team_round")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.status})"


class TeamRoundRespondent(models.Model):
    round = models.ForeignKey(TeamRound, on_delete=models.CASCADE, related_name="respondents")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_round_invites")
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["round", "user"], name="unique_team_round_respondent")]

    def __str__(self):
        return f"{self.user_id} in round {self.round_id}"


class TeamRoundAnswer(models.Model):
    respondent = models.ForeignKey(TeamRoundRespondent, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey("Question", on_delete=models.CASCADE)
    score = models.PositiveSmallIntegerField()  # 1..5
    note = models.TextField(blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["respondent", "question"], name="unique_team_round_answer")]
