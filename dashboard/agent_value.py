"""Aggregates what the 4 AI agents (Charlie/Nora/Theo/Milo) have actually
produced for a workspace, for the "Impact" tab in the Team hub.

Deliberately split into two trust tiers, kept as separate context
namespaces so a template can never render one as the other:

- `measured`: real numbers straight from the database -- documents created,
  AI-completed tasks, average completion duration, and AI cost derived from
  actual token counts. No assumptions involved.
- `estimated`: time/cost "saved vs. doing this manually", which cannot be
  measured (there is no manual baseline to compare against) and is instead
  derived from the sourced constants below -- real market data, not invented
  numbers, but still an estimate since no benchmark exists for this exact
  work. Tune them here, not in the template.
"""

import datetime

from django.db.models import Avg, Count, Sum
from django.utils import timezone

from gtm.agent_runtime import AGENT_DIRECTORY
from gtm.models import (
    ActionItem,
    ActionItemComment,
    AgentDocument,
    ChatMessage,
    WorkspaceChatMessage,
)
from gtm.models_workspace import WorkspaceActivityEvent

# Sourced 2026 market data, not a guess:
# - Marketing-strategist hourly rate: $150-300/hr (gtm8020.com, saasconsult.co,
#   strategicpete.com, marketerhire.com 2026 marketing-consultant-rate
#   research). Using the $225/hr midpoint -- ICP definition, SLA design, and
#   action-plan work require strategic judgment, not just execution-tier
#   freelance support (~$75/hr).
ASSUMED_HOURLY_RATE_USD = 225

# No industry survey publishes "hours to write an SLA/ICP one-pager" directly.
# Derived from the closest real proxy: professional business-writing speed of
# 1.5-2 hrs/page for researched, refined content (Wyzant business-writing-speed
# benchmarks), applied to a realistic ~2-page length for these tactical GTM
# documents -> ~4 hours/document. Applies to every AgentDocument regardless of
# how it was produced -- an AI-completed action item's deliverable is one of
# these documents, not a separate chunk of time on top of it.
ASSUMED_HOURS_PER_DOCUMENT = 4

# --- Gemini 2.5 Flash pricing (blended estimate; update if pricing changes) ---
GEMINI_FLASH_INPUT_COST_PER_1K_USD = 0.0000375
GEMINI_FLASH_OUTPUT_COST_PER_1K_USD = 0.00015

WEEKS_OF_TREND = 8


def _empty_context():
    return {
        "has_workspace": False,
        "measured": {
            "documents_total": 0,
            "documents_this_week": 0,
            "documents_this_month": 0,
            "documents_by_agent": [],
            "documents_by_agent_labels": [],
            "documents_by_agent_counts": [],
            "tasks_completed": 0,
            "tasks_attempted_not_completed": 0,
            "avg_duration_seconds": None,
            "ai_cost_usd": 0.0,
            "conversations_total": 0,
            "chat_task_actions": 0,
            "activity_labels": ["Conversations", "Task Actions via Chat", "Tasks Completed by AI", "Documents Created"],
            "activity_counts": [0, 0, 0, 0],
        },
        "estimated": {
            "hours_saved": 0.0,
            "cost_avoided_usd": 0.0,
            "hours_per_document": ASSUMED_HOURS_PER_DOCUMENT,
            "hourly_rate_usd": ASSUMED_HOURLY_RATE_USD,
        },
        "trend_weeks_labels": [],
        "trend_documents_per_week": [],
    }


def get_agent_value_context(current_workspace):
    """Per-workspace snapshot of AI agent output/value for the Impact tab."""
    if not current_workspace:
        return _empty_context()

    today = timezone.now().date()
    this_week_start = today - datetime.timedelta(days=today.weekday())
    this_month_start = today.replace(day=1)

    documents_qs = AgentDocument.objects.filter(workspace=current_workspace)
    documents_total = documents_qs.count()
    documents_this_week = documents_qs.filter(created_at__date__gte=this_week_start).count()
    documents_this_month = documents_qs.filter(created_at__date__gte=this_month_start).count()

    by_agent_counts = documents_qs.values("agent_type").annotate(count=Count("id"))
    counts_by_type = {row["agent_type"]: row["count"] for row in by_agent_counts}
    documents_by_agent = [
        {"agent_type": agent_type, "name": info["name"], "count": counts_by_type.get(agent_type, 0)}
        for agent_type, info in AGENT_DIRECTORY.items()
    ]

    tasks_completed = ActionItem.objects.filter(
        workspace=current_workspace,
        status="done",
        deliverable_document__isnull=False,
    ).count()

    ai_comments_qs = ActionItemComment.objects.filter(
        action_item__workspace=current_workspace,
        duration_ms__isnull=False,
    )
    tasks_attempted = ai_comments_qs.count()
    tasks_attempted_not_completed = max(tasks_attempted - tasks_completed, 0)

    avg_duration_ms = ai_comments_qs.aggregate(avg=Avg("duration_ms"))["avg"]
    avg_duration_seconds = round(avg_duration_ms / 1000, 1) if avg_duration_ms else None

    token_totals = ai_comments_qs.aggregate(
        prompt=Sum("prompt_token_count"),
        candidates=Sum("candidates_token_count"),
    )
    prompt_tokens = token_totals["prompt"] or 0
    candidates_tokens = token_totals["candidates"] or 0
    ai_cost_usd = (
        (prompt_tokens / 1000) * GEMINI_FLASH_INPUT_COST_PER_1K_USD
        + (candidates_tokens / 1000) * GEMINI_FLASH_OUTPUT_COST_PER_1K_USD
    )

    hours_saved = documents_total * ASSUMED_HOURS_PER_DOCUMENT
    cost_avoided_usd = hours_saved * ASSUMED_HOURLY_RATE_USD
    # Deliberately NOT showing cost_avoided/ai_cost as a "return multiple" --
    # with AI cost in fractions of a cent, that ratio is always an absurd,
    # multi-million-times figure regardless of how accurate the inputs are
    # (a near-zero denominator makes it swing wildly and read as fake). Two
    # honest, real-magnitude numbers (time saved, cost avoided) tell this
    # story better than a derived ratio that's technically correct but
    # impossible to believe at a glance.

    # Conversations: every chat turn across Charlie (session-scoped) and
    # Nora/Theo/Milo/Team (workspace-scoped) -- real usage volume, distinct
    # from documents/tasks since a conversation doesn't necessarily produce
    # either of those.
    conversations_total = (
        ChatMessage.objects.filter(session__workspace=current_workspace).count()
        + WorkspaceChatMessage.objects.filter(workspace=current_workspace).count()
    )

    # Task actions taken via a plain-language chat command (e.g. "move task X
    # to done") rather than clicking through the Tasks board -- tagged with
    # metadata__source='dashboard_agent' at the one place that logs them
    # (dashboard/parsers.py::_run_dashboard_action_command). Distinct from
    # tasks_completed above, which is the separate autonomous
    # complete-with-AI flow (gtm/action_item_completion.py).
    chat_task_actions = WorkspaceActivityEvent.objects.filter(
        workspace=current_workspace,
        metadata__source="dashboard_agent",
    ).count()

    activity_labels = ["Conversations", "Task Actions via Chat", "Tasks Completed by AI", "Documents Created"]
    activity_counts = [conversations_total, chat_task_actions, tasks_completed, documents_total]

    trend_weeks_labels = []
    trend_documents_per_week = []
    for i in range(WEEKS_OF_TREND - 1, -1, -1):
        week_start = this_week_start - datetime.timedelta(weeks=i)
        week_end = week_start + datetime.timedelta(days=6)
        count = documents_qs.filter(created_at__date__gte=week_start, created_at__date__lte=week_end).count()
        trend_weeks_labels.append(week_start.strftime("%b %d"))
        trend_documents_per_week.append(count)

    return {
        "has_workspace": True,
        "measured": {
            "documents_total": documents_total,
            "documents_this_week": documents_this_week,
            "documents_this_month": documents_this_month,
            "documents_by_agent": documents_by_agent,
            "documents_by_agent_labels": [row["name"] for row in documents_by_agent],
            "documents_by_agent_counts": [row["count"] for row in documents_by_agent],
            "tasks_completed": tasks_completed,
            "tasks_attempted_not_completed": tasks_attempted_not_completed,
            "avg_duration_seconds": avg_duration_seconds,
            "ai_cost_usd": round(ai_cost_usd, 4),
            "conversations_total": conversations_total,
            "chat_task_actions": chat_task_actions,
            "activity_labels": activity_labels,
            "activity_counts": activity_counts,
        },
        "estimated": {
            "hours_saved": round(hours_saved, 1),
            "cost_avoided_usd": round(cost_avoided_usd, 2),
            "hours_per_document": ASSUMED_HOURS_PER_DOCUMENT,
            "hourly_rate_usd": ASSUMED_HOURLY_RATE_USD,
        },
        "trend_weeks_labels": trend_weeks_labels,
        "trend_documents_per_week": trend_documents_per_week,
    }
