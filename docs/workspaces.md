# Collaborative Workspaces & RBAC

The GTM Validator implements a multi-tenant workspace architecture designed for high-security team collaboration. It utilizes a **Soft Isolation** pattern that enables seamless transitions from anonymous guest sessions to authenticated enterprise workspaces.

---

## 1. Multi-Tenant Infrastructure: The Workspace Model

The `Workspace` model in `gtm/models_workspace.py` acts as the primary organizational container.

### Implementation: Atomic Creation
When a user creates a workspace, the system uses an atomic transaction to ensure the creator is immediately assigned as the 'Admin'.

```python
@classmethod
def create_for_user(cls, name, user):
    """Atomic helper to ensure data integrity during workspace setup."""
    from django.db import transaction
    with transaction.atomic():
        workspace = cls.objects.create(name=name)
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=user,
            role='admin',
            is_active=True
        )
        return workspace
```

---

## 2. Role-Based Access Control (RBAC)

Permission logic is encapsulated in the `WorkspaceMembership` model through a five-tier role system.

### Implementation: RBAC Decorators
We utilize custom decorators in `gtm/decorators.py` to enforce security at the view level without duplicating code.

```python
def workspace_permission_required(permission_name):
    """
    Checks if a user has a specific Boolean permission (e.g., 'can_assign_tasks') 
    within the active workspace.
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            membership = WorkspaceMembership.objects.filter(
                user=request.user,
                workspace=request.workspace,
                is_active=True
            ).first()
            
            if not membership or not getattr(membership, permission_name):
                return HttpResponseForbidden("Permission Denied")
            
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
```

---

## 3. The "Soft Isolation" Handshake

One of the platform's non-obvious features is the ability to transfer an **Anonymous Session** to a **Workspace**.

### Handshake Logic
1.  **Identity:** Middleware identifies an anonymous user via a UUID cookie (`gtm_client`).
2.  **Assignment:** When the user registers or joins a workspace, the `AssessmentSession.user` and `AssessmentSession.workspace` fields are updated.
3.  **Security:** The system verifies the `owner_client_id` during the transfer to prevent "Session Hijacking."

---

## 4. Collaborative Activity Feed

The `WorkspaceActivityEvent` model provides a high-performance audit trail for the team.

-   **Structure:** Uses a lightweight `JSONField` for metadata.
-   **Usage:** Powers the "Recent Activity" sidebar and provides **Grounding Context** for the AI Chat Agent.
-   **Example Meta:** `{"old_status": "todo", "new_status": "done", "task_id": 42}`.

---

## 5. Global Guard: `safe_get_session_or_403`

Every assessment view depends on this centralized function to prevent **Horizontal Privilege Escalation**.

```python
def safe_get_session_or_403(request, session_id):
    """
    The Definitive Security Guard.
    1. Direct Owner: ALLOW.
    2. Active Workspace Member: ALLOW.
    3. Anonymous with matching Client ID: ALLOW.
    4. Otherwise: DENY (403).
    """
    session = get_object_or_404(AssessmentSession, pk=session_id)
    
    # Collaborative check
    if session.workspace:
        is_member = WorkspaceMembership.objects.filter(
            user=request.user, workspace=session.workspace, is_active=True
        ).exists()
        if is_member: return session, True
    
    # Private / Anonymous check
    if session.user == request.user or session.owner_client_id == _client_id(request):
        return session, True
        
    return None, False
```

## 6. Team Rounds (several people answer one assessment)

An admin or manager opens a round from the Workspace Hub and picks respondents. Each respondent answers the applicable questions privately (`TeamRoundAnswer` rows, not `AssessmentSession`s, so they never appear in "latest completed assessment" queries). Closing the round needs at least two submissions and creates one ordinary completed `AssessmentSession` in the workspace:

- **Score:** the respondents' average per question, rounded half up (`Response.score` is a whole 1-5 number everywhere in scoring). The round page keeps the exact average.
- **Disagreement:** a question whose answers are 2+ points apart is flagged. The flag and respondents' notes (without names) go into that response's `context_note`, so the AI insight for the question sees the disagreement.
- **Visibility:** answers are hidden while the round is open; after closing, every member sees each person's score by name. The answer page says so up front.

Code: `gtm/team_rounds.py` (rules), `gtm/models_team_rounds.py`, `dashboard/views/team_rounds.py`.

## 7. Deleting a Workspace or an Account

Workspace delete is permanent (it used to be a soft delete with no restore). Account deletion lives in `gtm/account_deletion.py`: a sole admin of a workspace that still has other members is blocked until they hand over admin; workspaces where the user is the only member are deleted with them; in shared workspaces, assessments and gaps the user created are detached (`user=NULL`) rather than cascaded away. Uploaded files are removed from storage after commit whenever the row that owns them is deleted (`gtm/storage_cleanup.py`). `python manage.py purge_stale_data` (dry run unless `--apply`) clears workspaces left by the old soft delete and files no row references.
