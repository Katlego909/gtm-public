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

from django.utils import timezone

from .models import ActionItem, ActionItemComment, AssessmentSession


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


def build_agent_action_tools(workspace, user, task_refs_sink: Optional[List[Any]] = None) -> List[Any]:
    """Build the shared set of workspace-collaboration tools available to
    any agent chatting on behalf of `user` within `workspace`.

    `task_refs_sink`, when given, is a mutable list that find_tasks and the
    task-resolving tools below (comment_on_task, assign_task,
    assign_task_to_agent) record real ActionItem ids into as they resolve
    them -- see agent_runtime.record_task_ref. Callers read this back after
    the turn to persist structured task grounding (WorkspaceChatMessage/
    ChatMessage.task_refs) independent of whatever text the model wrote."""
    from dashboard.parsers import _resolve_assignee, log_workspace_activity
    from dashboard.utils_notifications import send_notification
    from dashboard.views.helpers import _upsert_gap_metric_in_scope
    from dashboard.models import AIResourceRecommendation, GapAnalysisMetric, Resource
    from django.utils.dateparse import parse_date
    from .agent_dashboard_tools import _check_workspace_permission
    from .agent_runtime import record_task_ref

    def notify_teammate(username: str, title: str, message: str, link: str = "") -> str:
        """Send an in-app notification to a specific teammate in this
        workspace. `username` can be a name, username, or email. Use this to
        flag something to a specific person instead of only replying in chat.
        """
        denial = _check_workspace_permission(workspace, user)
        if denial:
            return denial
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
        to the team on the Tasks board. Use find_tasks first if you don't
        already have this task's real ID. Use this to share findings or ask
        a question about a specific task without changing its status.
        """
        denial = _check_workspace_permission(workspace, user)
        if denial:
            return denial
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."
        record_task_ref(task_refs_sink, item.id, item.note)
        ActionItemComment.objects.create(action_item=item, user=user, text=comment_text)
        log_workspace_activity(
            workspace, user, 'comment_added',
            f"{user.get_full_name() or user.username} commented on task '{item.note[:80]}'.",
            object_type='action_item', object_id=item.id,
        )
        return f'Commented on "{item.note[:50]}".'

    def assign_task(action_item_id: str, assignee: str) -> str:
        """Assign a task (ActionItem) to a specific teammate. Use find_tasks
        first if you don't already have this task's real ID. `assignee` can
        be a name, username, or email. Only usable if the requesting user
        has task-assignment permission in this workspace.
        """
        denial = _check_workspace_permission(workspace, user, permission="can_assign_tasks")
        if denial:
            return denial
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."
        record_task_ref(task_refs_sink, item.id, item.note)
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
        (Nora), resource (Theo), insights (Milo). Use find_tasks first if
        you don't already have this task's real ID. Only usable if the
        requesting user has task-assignment permission in this workspace.
        """
        from .agent_runtime import AGENT_DIRECTORY

        if agent_type not in AGENT_DIRECTORY:
            valid = ", ".join(AGENT_DIRECTORY.keys())
            return f"'{agent_type}' isn't a recognized agent type. Use one of: {valid}."

        denial = _check_workspace_permission(workspace, user, permission="can_assign_tasks")
        if denial:
            return denial
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."
        record_task_ref(task_refs_sink, item.id, item.note)

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

    def find_tasks(query: str = "", status: str = "", assignee: str = "", created_on: str = "") -> str:
        """Search for tasks (ActionItems) in this workspace and get back
        their real IDs. ALWAYS call this before commenting on, assigning, or
        routing a task unless you already have its real ID from earlier in
        this same conversation -- never guess or invent one. `query`
        matches (partial, case-insensitive) against the task's note text.
        `status` is one of: todo, doing, done (leave blank to search open
        tasks only, i.e. todo + doing). `assignee` filters to tasks
        already assigned to a specific teammate (name, username, or
        email). `created_on` is an exact date in YYYY-MM-DD format if the
        user referenced a specific day (e.g. 'the task from July 16th' --
        use today's date, given in your instructions, to resolve the
        year). Returns up to 10 matches, most recently created first.
        """
        from .agent_runtime import AGENT_DIRECTORY

        qs = ActionItem.objects.filter(workspace=workspace)
        if status:
            qs = qs.filter(status=status)
        else:
            qs = qs.exclude(status="done")
        if query.strip():
            qs = qs.filter(note__icontains=query.strip())
        if assignee.strip():
            recipient = _resolve_assignee(workspace, assignee)
            if not recipient:
                return _agent_name_hint(assignee) or f"I couldn't find a teammate matching '{assignee}' in this workspace."
            qs = qs.filter(assigned_to=recipient)
        if created_on.strip():
            parsed_date = parse_date(created_on.strip())
            if parsed_date:
                qs = qs.filter(created_at__date=parsed_date)

        tasks = list(qs.select_related("assigned_to").order_by("-created_at")[:10])
        if not tasks:
            return "No matching tasks found in this workspace."

        lines = ["Matching tasks:"]
        for item in tasks:
            record_task_ref(task_refs_sink, item.id, item.note)
            if item.assigned_to:
                assignee_label = item.assigned_to.get_full_name() or item.assigned_to.username
            elif item.assigned_agent_type:
                assignee_label = AGENT_DIRECTORY.get(item.assigned_agent_type, {}).get("name", item.assigned_agent_type)
            else:
                assignee_label = "Unassigned"
            lines.append(
                f"- [ID: {item.id}] {item.note} ({item.get_status_display()}, "
                f"created {item.created_at.strftime('%b %d, %Y')}, {assignee_label})"
            )
        return "\n".join(lines)

    def escalate_gap(category: str, metric: str, current: float, target: float, recommendation: str) -> str:
        """Flag a GTM gap for human review in the Gap Analysis panel. This
        does NOT auto-create tasks -- a teammate still has to accept it there
        first. `category` must be one of: Lead Generation, Sales Efficiency,
        Customer Success, Product Marketing, Sales Velocity, Marketing ROI.
        `metric` must be one of: Monthly Qualified Leads, Average Deal Size,
        Net Revenue Retention, Product Qualified Leads, Win Rate, CAC Payback Period.
        """
        denial = _check_workspace_permission(workspace, user)
        if denial:
            return denial
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
        denial = _check_workspace_permission(workspace, user)
        if denial:
            return denial
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

    def _hubspot_or_message(workspace):
        """(client, None) if HubSpot is connected for this workspace, else
        (None, an explanatory message) safe to return directly from a tool."""
        from .integrations.hubspot_client import get_client_for_workspace

        client = get_client_for_workspace(workspace)
        if not client:
            return None, (
                "This workspace doesn't have HubSpot connected yet. Ask a workspace "
                "admin to add a HubSpot Private App token under Workspace Settings > Integrations."
            )
        return client, None

    def lookup_crm_company(company_identifier: str) -> str:
        """Look up a company in the workspace's connected HubSpot CRM by
        domain or name, to pull real context (industry, size, lifecycle
        stage) into a GTM insight, playbook, or chat answer. Read-only.
        Requires HubSpot to be connected under Workspace Settings > Integrations.
        """
        denial = _check_workspace_permission(workspace, user)
        if denial:
            return denial
        from .integrations.hubspot_client import HubSpotAPIError

        client, error = _hubspot_or_message(workspace)
        if error:
            return error
        try:
            company = client.find_company(company_identifier)
        except HubSpotAPIError as exc:
            return f"HubSpot lookup failed: {exc}"
        if company is None:
            return f"I couldn't find a single HubSpot company matching '{company_identifier}' -- try a more specific name or domain."
        props = company.get('properties', {})
        parts = [f"Name: {props.get('name', 'Unknown')}"]
        for label, key in [("Domain", "domain"), ("Industry", "industry"),
                            ("Employees", "numberofemployees"), ("Lifecycle stage", "lifecyclestage")]:
            if props.get(key):
                parts.append(f"{label}: {props[key]}")
        return " | ".join(parts)

    def log_insight_to_crm(company_identifier: str, summary: str) -> str:
        """Write a GTM finding as a Note on the matching HubSpot company
        record (domain or name), so the workspace's HubSpot users see it
        without leaving their CRM. Requires HubSpot to be connected and the
        requesting user to have task-assignment permission in this
        workspace (same requirement as create_crm_follow_up_task).
        """
        denial = _check_workspace_permission(workspace, user, permission="can_assign_tasks")
        if denial:
            return denial
        from .integrations.hubspot_client import HubSpotAPIError

        client, error = _hubspot_or_message(workspace)
        if error:
            return error
        try:
            company = client.find_company(company_identifier)
            if company is None:
                return f"I couldn't find a single HubSpot company matching '{company_identifier}' -- try a more specific name or domain."
            client.create_note(company['id'], f"[ForgeGTM AI] {summary}")
        except HubSpotAPIError as exc:
            return f"HubSpot write failed: {exc}"
        company_name = company.get('properties', {}).get('name', company_identifier)
        return f'Logged a note to HubSpot on "{company_name}".'

    def create_crm_follow_up_task(action_item_id: str, company_identifier: str, due_date: str = "") -> str:
        """Push a workspace task (ActionItem) into HubSpot as a Task on the
        matching company (domain or name), so the client's team sees the
        GTM follow-up inside HubSpot itself. `due_date` is optional, format
        YYYY-MM-DD. Re-running this on an already-synced task updates the
        existing HubSpot task rather than duplicating it. Requires HubSpot
        to be connected and the requesting user to have task-assignment
        permission in this workspace (same requirement as assign_task).
        """
        from .integrations.hubspot_client import HubSpotAPIError

        denial = _check_workspace_permission(workspace, user, permission="can_assign_tasks")
        if denial:
            return denial
        try:
            item = ActionItem.objects.get(pk=int(action_item_id), workspace=workspace)
        except (ActionItem.DoesNotExist, ValueError, TypeError):
            return f"I couldn't find task {action_item_id} in this workspace."

        client, error = _hubspot_or_message(workspace)
        if error:
            return error

        subject = item.note[:100]
        body = f"GTM follow-up from ForgeGTM: {item.note}"
        try:
            company = client.find_company(company_identifier)
            if company is None:
                return f"I couldn't find a single HubSpot company matching '{company_identifier}' -- try a more specific name or domain."
            if item.external_crm_task_id:
                client.update_task(item.external_crm_task_id, subject, body, due_date or None)
                action_verb = "Updated"
            else:
                item.external_crm_task_id = client.create_task(company['id'], subject, body, due_date or None)
                action_verb = "Pushed"
        except HubSpotAPIError as exc:
            item.crm_sync_status = 'error'
            item.save(update_fields=['crm_sync_status'])
            return f"HubSpot task sync failed: {exc}"

        item.crm_sync_status = 'synced'
        item.crm_synced_at = timezone.now()
        item.save(update_fields=['external_crm_task_id', 'crm_sync_status', 'crm_synced_at'])
        company_name = company.get('properties', {}).get('name', company_identifier)
        return f'{action_verb} "{subject}" to HubSpot as a task on {company_name}.'

    return [
        notify_teammate, comment_on_task, assign_task, assign_task_to_agent, find_tasks, escalate_gap,
        recommend_resource, lookup_crm_company, log_insight_to_crm, create_crm_follow_up_task,
    ]
