"""Service layer for agent-style GTM execution workflows."""

from __future__ import annotations

from datetime import timedelta
import re
from typing import Any, Dict, List

from django.utils import timezone

from .models import ActionItem, AssessmentSession, Response


CATEGORY_FALLBACK_TASKS = {
    "Demand": "Launch one repeatable weekly demand-generation activity and track lead volume.",
    "Conversion": "Standardize lead qualification, handoff, and response timing for every new lead.",
    "Delivery": "Document onboarding and delivery steps with owners, timelines, and customer checkpoints.",
}


QUESTION_TASK_PATTERNS = [
    (r"onboarding|get started|welcome", "Document onboarding steps, assign owners, and send a consistent welcome sequence."),
    (r"win|lose deals|win/loss", "Track win-loss reasons monthly and review one conversion fix with the team."),
    (r"reply|response|follow up|lead", "Set a lead response SLA and review compliance every week."),
    (r"qualif|fit questions|budget|decision maker", "Create a standard qualification checklist for every sales conversation."),
    (r"content|newsletter|outreach|marketing", "Commit to one weekly marketing motion and measure leads generated from it."),
    (r"testimonial|case stud", "Collect one recent customer proof point and add it to sales materials."),
    (r"renewal|retention|loyalty", "Define one retention touchpoint and assign ownership for customer follow-up."),
    (r"survey|feedback", "Launch a lightweight customer feedback loop and review responses every month."),
]


def _normalize_task_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized:
        return ""
    normalized = normalized[0].upper() + normalized[1:]
    if not normalized.endswith("."):
        normalized += "."
    return normalized


def _task_from_response(response: Response) -> str:
    question_text = (response.question.text or "").lower()

    for pattern, task in QUESTION_TASK_PATTERNS:
        if re.search(pattern, question_text):
            return task

    if response.question.diagnostic_note:
        return _normalize_task_text(response.question.diagnostic_note)

    return CATEGORY_FALLBACK_TASKS.get(
        response.question.category.name,
        "Define a repeatable process, assign ownership, and track progress weekly.",
    )


def build_execution_plan(
    session: AssessmentSession,
    actor=None,
    persist: bool = True,
    limit: int = 5,
) -> Dict[str, Any]:
    """Create or suggest prioritized execution tasks from low-scoring responses."""
    from .views import _band_for_score, _compute_scores

    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)

    low_responses = list(
        Response.objects.filter(session=session, score__lte=2)
        .select_related("question__category")
        .order_by("score", "question__id_code")
    )
    existing_actions = list(ActionItem.objects.filter(session=session).select_related("question"))
    existing_notes = {action.note.strip().lower() for action in existing_actions if action.note.strip()}

    created_items: List[ActionItem] = []
    skipped_items: List[str] = []
    suggested_items: List[Dict[str, Any]] = []

    base_due_date = timezone.localdate()

    for index, response in enumerate(low_responses, start=1):
        task_note = _normalize_task_text(_task_from_response(response))
        if not task_note:
            continue

        task_key = task_note.lower()
        if task_key in existing_notes:
            skipped_items.append(task_note)
            continue

        suggested_items.append(
            {
                "note": task_note,
                "question": response.question,
                "response": response,
                "due_date": base_due_date + timedelta(days=7 * index),
            }
        )
        existing_notes.add(task_key)

        if len(suggested_items) >= limit:
            break

    if persist:
        for item in suggested_items:
            created_items.append(
                ActionItem.objects.create(
                    session=session,
                    workspace=session.workspace,
                    question=item["question"],
                    note=item["note"],
                    created_by=actor if getattr(actor, "is_authenticated", False) else session.user,
                    due_date=item["due_date"],
                )
            )

    top_categories = sorted(cat_scores, key=lambda row: row["avg"])[:3]

    return {
        "overall_score": round(overall, 1),
        "stage": band.stage if band else "Unknown",
        "top_categories": [row["category"].name for row in top_categories],
        "created_items": created_items,
        "created_count": len(created_items),
        "suggested_items": suggested_items,
        "skipped_items": skipped_items,
        "has_critical_gaps": bool(low_responses),
        "existing_action_count": len(existing_actions),
    }


def review_action_items(session: AssessmentSession, limit: int = 6) -> Dict[str, Any]:
    """Summarize the current action-item backlog for chat and agent responses."""
    actions = ActionItem.objects.filter(session=session).select_related("question", "assigned_to")
    open_actions = list(actions.exclude(status="done").order_by("due_date", "created_at")[:limit])
    completed_count = actions.filter(status="done").count()
    in_progress_count = actions.filter(status="doing").count()
    todo_count = actions.filter(status="todo").count()

    overdue_actions = [
        action for action in open_actions
        if action.due_date and action.due_date < timezone.localdate()
    ]

    return {
        "total": actions.count(),
        "todo": todo_count,
        "in_progress": in_progress_count,
        "done": completed_count,
        "open_actions": open_actions,
        "overdue_count": len(overdue_actions),
    }