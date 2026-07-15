"""
Tools that let an agent take a real, visible action in the workspace --
notify a teammate, comment on or assign a task, escalate a gap for human
review, or recommend a resource -- rather than only writing documents or
prose back into the chat. Mirrors the shared-tool-builder shape of
`ai_chat._build_document_tools_for_session`: a closure-based builder
returning plain functions whose type hints/docstrings become the Gemini
tool schema via automatic function calling.
"""

from typing import Any, List, Optional

from .models import ActionItem, ActionItemComment, AssessmentSession
from .models_workspace import WorkspaceMembership


def _agent_name_hint(candidate: str, *, action: str = "consult") -> Optional[str]:
    """If `candidate` matches a fellow AI agent's display name (Charlie/Nora/
    Theo/Milo), return a corrective message pointing at the right tool.
    Fellow agents aren't WorkspaceMembership users -- there's no inbox to
    notify -- so a plain "not found" error would just leave the model
    retrying a human lookup that can never succeed.

    `action="consult"` (the default, used by notify_teammate) points at the
    matching consult_ tool. `action="assign"` (used by assign_task) points at
    assign_task_to_agent instead, since that's the real mechanism for
    routing a task to a fellow agent.
    """
    from .agent_runtime import AGENT_DIRECTORY

    needle = candidate.strip().lower()
    for agent_type, info in AGENT_DIRECTORY.items():
        if info['name'].lower() == needle:
            if action == "assign":
                return (
                    f"{info['name']} is a fellow AI agent (the {info['label']}), not a human "
                    f"teammate. Use assign_task_to_agent instead to route this task to them."
                )
            return (
                f"{info['name']} is a fellow AI agent (the {info['label']}), not a human "
                f"teammate -- there's no inbox to notify. Use consult_{agent_type}_agent instead "
                f"to ask {info['name']} directly and weave the answer into your own reply."
            )
    return None


def build_agent_action_tools(workspace, user) -> List[Any]:
    """Build the shared set of workspace-collaboration tools available to
    any agent chatting on behalf of `user` within `workspace`."""
    from dashboard.parsers import _resolve_assignee, log_workspace_activity
    from dashboard.utils_notifications import send_notification
    from dashboard.views.helpers import _upsert_gap_metric_in_scope
    from dashboard.models import AIResourceRecommendation, GapAnalysisMetric, Resource

    def notify_teammate(username: str, title: str, message: str, link: str = "") -> str:
        """Send an in-app notification to a specific teammate in this
        workspace. `username` can be a name, username, or email. Use this to
        flag something to a specific person instead of only replying in chat.
        """
        recipient = _resolve_assignee(workspace, username)
        if not recipient:
            return _agent_name_hint(username) or f"I couldn't find a teammate matching '{username}' in this workspace."
        send_notification(
            recipient=recipient, sender=user, workspace=workspace,
            notification_type='ai_report', level='info',
            title=title, message=message, link=link,
        )
        return f"Notified {recipient.get_full_name() or recipient.username}."

    def comment_on_task(action_item_id: str, comment_text: str) -> str:
        """Post a comment on a task (ActionItem) in this workspace, visible
        to the team on the Tasks board. Use this to share findings or ask a
        question about a specific task without changing its status.
        """
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."
        ActionItemComment.objects.create(action_item=item, user=user, text=comment_text)
        log_workspace_activity(
            workspace, user, 'comment_added',
            f"{user.get_full_name() or user.username} commented on task '{item.note[:80]}'.",
            object_type='action_item', object_id=item.id,
        )
        return f'Commented on "{item.note[:50]}".'

    def assign_task(action_item_id: str, assignee: str) -> str:
        """Assign a task (ActionItem) to a specific teammate. `assignee` can
        be a name, username, or email. Only usable if the requesting user
        has task-assignment permission in this workspace.
        """
        membership = WorkspaceMembership.objects.filter(
            workspace=workspace, user=user, is_active=True
        ).first()
        if not membership or not membership.can_assign_tasks:
            return "You don't have permission to assign tasks in this workspace."
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."
        recipient = _resolve_assignee(workspace, assignee)
        if not recipient:
            hint = _agent_name_hint(assignee, action="assign")
            return hint or f"I couldn't find a teammate matching '{assignee}' in this workspace."
        item.assigned_to = recipient
        item.save()
        return f'Assigned "{item.note[:50]}" to {recipient.get_full_name() or recipient.username}.'

    def assign_task_to_agent(action_item_id: str, agent_type: str) -> str:
        """Route a task (ActionItem) to one of the 4 fellow AI agents so they
        actually attempt it -- this both labels the task as theirs (visible
        on the Tasks board) and kicks off their real completion attempt in
        the background, the same as a human clicking "Complete with AI".
        `agent_type` must be one of: gtm_strategist (Charlie), portfolio
        (Nora), resource (Theo), insights (Milo). Only usable if the
        requesting user has task-assignment permission in this workspace.
        """
        from .agent_runtime import AGENT_DIRECTORY

        if agent_type not in AGENT_DIRECTORY:
            valid = ", ".join(AGENT_DIRECTORY.keys())
            return f"'{agent_type}' isn't a recognized agent type. Use one of: {valid}."

        membership = WorkspaceMembership.objects.filter(
            workspace=workspace, user=user, is_active=True
        ).first()
        if not membership or not membership.can_assign_tasks:
            return "You don't have permission to assign tasks in this workspace."
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."

        agent_name = AGENT_DIRECTORY[agent_type]['name']
        item.assigned_agent_type = agent_type
        item.save(update_fields=['assigned_agent_type'])
        log_workspace_activity(
            workspace, user, 'task_moved',
            f"{user.get_full_name() or user.username} routed task '{item.note[:80]}' to {agent_name}.",
            object_type='action_item', object_id=item.id,
        )

        if item.session_id is None:
            return f"Routed \"{item.note[:50]}\" to {agent_name}, but it has no linked assessment so they can't attempt it yet."
        if item.status != 'todo':
            return f"Routed \"{item.note[:50]}\" to {agent_name}. It's already {item.get_status_display()}, so they won't re-attempt it."

        from django.core.cache import cache
        from gtm.ai_services import _acquire_lock
        from gtm.utils_async import run_in_background
        from dashboard.views.actions import (
            _action_item_complete_lock_key,
            _action_item_complete_status_cache_key,
            _complete_action_item_background,
        )

        lock_key = _action_item_complete_lock_key(item.id)
        if not _acquire_lock(lock_key, ttl_seconds=180):
            return f"Routed \"{item.note[:50]}\" to {agent_name} -- they're already working on it."

        status_key = _action_item_complete_status_cache_key(item.id)
        cache.set(status_key, {"state": "running"}, timeout=600)
        run_in_background(
            _complete_action_item_background,
            item.id, user.id, workspace.id,
            name=f"action_item_complete:{item.id}",
        )
        return f'Routed "{item.note[:50]}" to {agent_name} -- they\'re attempting it now.'

    def escalate_gap(category: str, metric: str, current: float, target: float, recommendation: str) -> str:
        """Flag a GTM gap for human review in the Gap Analysis panel. This
        does NOT auto-create tasks -- a teammate still has to accept it there
        first. `category` must be one of: Lead Generation, Sales Efficiency,
        Customer Success, Product Marketing, Sales Velocity, Marketing ROI.
        `metric` must be one of: Monthly Qualified Leads, Average Deal Size,
        Net Revenue Retention, Product Qualified Leads, Win Rate, CAC Payback Period.
        """
        if category not in dict(GapAnalysisMetric.CATEGORY_CHOICES):
            return f"'{category}' isn't a recognized gap category."
        if metric not in dict(GapAnalysisMetric.METRIC_CHOICES):
            return f"'{metric}' isn't a recognized metric."
        _upsert_gap_metric_in_scope(
            user=user, workspace=workspace, session=None,
            payload={
                'category': category, 'metric': metric,
                'current': current, 'target': target,
                'priority': 'Medium', 'recommendation': recommendation,
            },
            source='AI',
        )
        return f"Flagged '{metric}' as a gap for the team to review."

    def recommend_resource(resource_name: str, session_id: str, rationale: str) -> str:
        """Recommend an existing workspace Resource (from the Library) for a
        specific assessment session. Use a list/search tool first if you
        don't already know the resource name or session ID.
        """
        try:
            session = AssessmentSession.objects.get(uuid=session_id, workspace=workspace)
        except (AssessmentSession.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find assessment session {session_id} in this workspace."
        resource = Resource.objects.filter(workspace=workspace, name__icontains=resource_name).first()
        if not resource:
            return f"I couldn't find a resource matching '{resource_name}' in this workspace's Library."
        AIResourceRecommendation.objects.get_or_create(
            session=session, resource=resource,
            defaults={'rationale': rationale[:255]},
        )
        return f'Recommended "{resource.name}" for this assessment.'

    return [notify_teammate, comment_on_task, assign_task, assign_task_to_agent, escalate_gap, recommend_resource]
