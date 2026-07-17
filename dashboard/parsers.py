import re
from django.db import models
from gtm.models import ActionItem, ActionItemComment
from gtm.models_workspace import WorkspaceMembership, WorkspaceActivityEvent

def log_workspace_activity(workspace, actor, event_type, summary, object_type='', object_id='', metadata=None, session=None):
    """Create a workspace activity event if workspace context is available."""
    if not workspace:
        return
    WorkspaceActivityEvent.objects.create(
        workspace=workspace,
        session=session,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        event_type=event_type,
        summary=summary,
        object_type=object_type or '',
        object_id=str(object_id) if object_id else '',
        metadata=metadata or {},
    )


def _extract_create_task_command(message):
    """Parse simple dashboard action commands like: add a task called "X" and assign to Y."""
    if not message:
        return None

    text = message.strip()

    quoted_pattern = re.compile(
        r"^(?:add|create)\s+(?:a\s+)?task(?:\s+(?:called|named))?\s+[\"'](?P<title>[^\"']+)[\"'](?:\s+and\s+assign(?:\s+it)?\s+to\s+(?P<assignee>.+))?$",
        re.IGNORECASE,
    )
    plain_pattern = re.compile(
        r"^(?:add|create)\s+(?:a\s+)?task(?:\s+(?:called|named))?\s+(?P<title>[^\n,.]+?)(?:\s+and\s+assign(?:\s+it)?\s+to\s+(?P<assignee>[^\n,.]+))?$",
        re.IGNORECASE,
    )

    match = quoted_pattern.match(text) or plain_pattern.match(text)
    if not match:
        return None

    title = (match.group('title') or '').strip().strip('"\'')
    assignee = (match.group('assignee') or '').strip().strip('"\'')
    if not title:
        return None

    return {
        'action': 'create_task',
        'title': title[:240],
        'assignee': assignee,
    }


def _normalize_task_status(raw_status):
    if not raw_status:
        return None
    cleaned = raw_status.strip().lower().replace('-', ' ').replace('_', ' ')
    mapping = {
        'todo': 'todo',
        'to do': 'todo',
        'backlog': 'todo',
        'doing': 'doing',
        'in progress': 'doing',
        'progress': 'doing',
        'done': 'done',
        'complete': 'done',
        'completed': 'done',
        'finish': 'done',
        'finished': 'done',
    }
    return mapping.get(cleaned)


def _extract_move_task_command(message):
    if not message:
        return None
    text = message.strip()

    patterns = [
        re.compile(
            r"^(?:move|set|update|change)\s+task\s+(?P<target>.+?)\s+(?:to|as)\s+(?P<status>todo|to do|doing|in progress|done|complete|completed|finished)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:move|set|update|change)\s+[\"'](?P<target>[^\"']+)[\"']\s+(?:to|as)\s+(?P<status>todo|to do|doing|in progress|done|complete|completed|finished)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:mark)\s+task\s+(?P<target>.+?)\s+(?:as\s+)?(?P<status>done|complete|completed|finished|doing|in progress|todo|to do)$",
            re.IGNORECASE,
        ),
    ]

    match = None
    for pattern in patterns:
        match = pattern.match(text)
        if match:
            break
    if not match:
        return None

    status = _normalize_task_status(match.group('status'))
    target = (match.group('target') or '').strip().strip('"\'')
    if not target or not status:
        return None

    return {
        'action': 'move_task',
        'target': target,
        'status': status,
    }


def _extract_delete_task_command(message):
    if not message:
        return None
    text = message.strip()

    patterns = [
        re.compile(r"^(?:delete|remove)\s+task\s+[\"'](?P<target>[^\"']+)[\"']$", re.IGNORECASE),
        re.compile(r"^(?:delete|remove)\s+task\s+(?P<target>.+)$", re.IGNORECASE),
    ]
    match = None
    for pattern in patterns:
        match = pattern.match(text)
        if match:
            break
    if not match:
        return None

    target = (match.group('target') or '').strip().strip('"\'')
    return {'action': 'delete_task', 'target': target} if target else None


def _extract_comment_task_command(message):
    if not message:
        return None
    text = message.strip()

    patterns = [
        re.compile(
            r"^(?:add\s+)?comment\s+(?:on|to)\s+task\s+(?P<target>.+?)\s*:\s*(?P<comment>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:add\s+)?comment\s+[\"'](?P<comment>[^\"']+)[\"']\s+(?:on|to)\s+task\s+(?P<target>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:add\s+)?note\s+(?:on|to)\s+task\s+(?P<target>.+?)\s*:\s*(?P<comment>.+)$",
            re.IGNORECASE,
        ),
    ]
    match = None
    for pattern in patterns:
        match = pattern.match(text)
        if match:
            break
    if not match:
        return None

    target = (match.group('target') or '').strip().strip('"\'')
    comment = (match.group('comment') or '').strip().strip('"\'')
    if not target or not comment:
        return None

    return {
        'action': 'comment_task',
        'target': target,
        'comment': comment[:500],
    }


def _resolve_assignee(current_workspace, assignee_text):
    """Resolve assignee name/email to a user in the current workspace."""
    if not current_workspace or not assignee_text:
        return None

    candidate = assignee_text.strip()
    memberships = WorkspaceMembership.objects.filter(
        workspace=current_workspace,
        is_active=True,
    ).select_related('user')

    for membership in memberships:
        user = membership.user
        full_name = (user.get_full_name() or '').strip().lower()
        username = (user.username or '').strip().lower()
        email = (user.email or '').strip().lower()
        check = candidate.lower()
        if check in {full_name, username, email}:
            return user

    matches = memberships.filter(
        models.Q(user__first_name__icontains=candidate)
        | models.Q(user__last_name__icontains=candidate)
        | models.Q(user__username__icontains=candidate)
        | models.Q(user__email__icontains=candidate)
    )
    membership = matches.first()
    return membership.user if membership else None


def _task_command_queryset(request, session, current_workspace):
    if current_workspace:
        return ActionItem.objects.filter(workspace=current_workspace)
    return ActionItem.objects.filter(
        workspace__isnull=True,
        session__user=request.user,
    )


def _resolve_task_for_command(request, session, current_workspace, target):
    """Resolve a task by id or title within current workspace/personal scope."""
    base_qs = _task_command_queryset(request, session, current_workspace)
    token = (target or '').strip().strip('"\'')
    if not token:
        return None

    if token.isdigit():
        return base_qs.filter(pk=int(token)).first()

    by_exact = base_qs.filter(note__iexact=token).order_by('-updated_at')
    if by_exact.exists():
        return by_exact.first()

    session_first = base_qs.filter(session=session, note__icontains=token).order_by('-updated_at')
    if session_first.exists():
        return session_first.first()

    return base_qs.filter(note__icontains=token).order_by('-updated_at').first()


def _run_dashboard_action_command(request, session, current_workspace, message):
    """Execute deterministic dashboard commands before free-form AI chat."""
    cmd = (
        _extract_create_task_command(message)
        or _extract_move_task_command(message)
        or _extract_delete_task_command(message)
        or _extract_comment_task_command(message)
    )
    if not cmd:
        return None

    actor_name = request.user.get_full_name() or request.user.username

    if cmd['action'] == 'create_task':
        assigned_user = _resolve_assignee(current_workspace, cmd['assignee'])
        item, was_created = ActionItem.objects.create_deduped(
            session=session,
            workspace=current_workspace,
            note=cmd['title'],
            status='todo',
            created_by=request.user,
            assigned_to=assigned_user,
            owner=(assigned_user.get_full_name() if assigned_user else actor_name),
        )

        assignee_text = assigned_user.get_full_name() or assigned_user.username if assigned_user else None
        if was_created and current_workspace:
            summary = (
                f"{actor_name} created task '{item.note}'"
                + (f" and assigned it to {assignee_text}." if assignee_text else ".")
            )
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_created',
                summary,
                object_type='action_item',
                object_id=item.id,
                metadata={'source': 'dashboard_agent', 'assigned_to': assignee_text or ''},
                session=item.session,
            )

        if not was_created:
            response_text = f"That task already exists: **{item.note}**."
        elif cmd['assignee'] and not assigned_user and current_workspace:
            response_text = (
                f"Task created: **{item.note}** (To do).\n"
                f"I could not find **{cmd['assignee']}** in this workspace, so it is currently unassigned."
            )
        elif assignee_text:
            response_text = f"Task created: **{item.note}** and assigned to **{assignee_text}**."
        else:
            response_text = f"Task created: **{item.note}** (To do)."

        return {
            'success': True,
            'response': response_text,
            'intent': 'dashboard_action',
            'action': {
                'type': 'create_task',
                'task_id': item.id,
                'assigned_to': assignee_text,
            }
        }

    task = _resolve_task_for_command(request, session, current_workspace, cmd.get('target'))
    if not task:
        return {
            'success': True,
            'response': "I could not find that task in your current workspace context. Try using the task ID or exact title.",
            'intent': 'dashboard_action',
            'action': {'type': 'not_found'},
        }

    if cmd['action'] == 'move_task':
        if current_workspace:
            membership = WorkspaceMembership.objects.filter(
                user=request.user,
                workspace=current_workspace,
                is_active=True,
            ).first()
            if not membership or not membership.can_assign_tasks:
                return {
                    'success': True,
                    'response': "You do not have permission to move tasks in this workspace.",
                    'intent': 'dashboard_action',
                    'action': {'type': 'permission_denied'},
                }

        task.status = cmd['status']
        task.save(update_fields=['status', 'updated_at'])

        if current_workspace:
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_moved',
                f"{actor_name} moved task '{task.note[:80]}' to {task.get_status_display()}.",
                object_type='action_item',
                object_id=task.id,
                metadata={'source': 'dashboard_agent', 'status': task.status},
                session=task.session,
            )

        return {
            'success': True,
            'response': f"Moved **{task.note}** to **{task.get_status_display()}**.",
            'intent': 'dashboard_action',
            'action': {'type': 'move_task', 'task_id': task.id, 'status': task.status},
        }

    if cmd['action'] == 'delete_task':
        task_id = task.id
        task_note = task.note
        task_session = task.session
        task.delete()

        if current_workspace:
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_deleted',
                f"{actor_name} deleted task '{task_note[:80]}'.",
                object_type='action_item',
                object_id=task_id,
                metadata={'source': 'dashboard_agent'},
                session=task_session,
            )

        return {
            'success': True,
            'response': f"Deleted task **{task_note}**.",
            'intent': 'dashboard_action',
            'action': {'type': 'delete_task', 'task_id': task_id},
        }

    if cmd['action'] == 'comment_task':
        comment = ActionItemComment.objects.create(
            action_item=task,
            user=request.user,
            text=cmd['comment'],
        )

        if current_workspace:
            log_workspace_activity(
                current_workspace,
                request.user,
                'comment_added',
                f"{actor_name} commented on task '{task.note[:80]}'.",
                object_type='action_item',
                object_id=task.id,
                metadata={'source': 'dashboard_agent', 'comment_id': str(comment.id)},
                session=task.session,
            )

        return {
            'success': True,
            'response': f"Added comment to **{task.note}**.",
            'intent': 'dashboard_action',
            'action': {'type': 'comment_task', 'task_id': task.id, 'comment_id': str(comment.id)},
        }

    return None
