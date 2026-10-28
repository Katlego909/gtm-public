"""Deterministic automations and the scheduled job that runs them.

`python manage.py run_automations` (Cloud Run Job, triggered hourly by Cloud
Scheduler) calls run_due_automations and sweep_stale_runs. Every rule is
plain Python over the database -- no AI calls, no credits -- so a rule
behaves the same every time and costs nothing to keep running.

Running is idempotent: each automation is claimed with a lock and its
next_run_at is advanced before it acts, so overlapping jobs never act twice.
"""

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone

from .models import ActionItem, AgentRun
from .models_automations import Automation, AutomationRun

logger = logging.getLogger(__name__)

INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1), "monthly": timedelta(days=30)}
# A planning/running agent run untouched this long, with no worker holding
# its lock, has lost its thread (deploy, crash) -- fail it so it can be retried.
STALE_RUN_AFTER = timedelta(minutes=15)
AUTOMATION_LOCK_TTL_SECONDS = 300


class AutomationError(Exception):
    """A readable reason an automation can't be created as specified."""


def automation_lock_key(automation_id) -> str:
    return f"gtm:automation:{automation_id}:lock"


# ================================================================
# Creating
# ================================================================

def _resolve_people(workspace, names: List[str]) -> Tuple[List[Any], List[str]]:
    """Users for `names` (name, username, email, or "admins"), plus the names
    that matched nobody."""
    from dashboard.parsers import _resolve_assignee
    from .models_workspace import WorkspaceMembership

    people, unknown = [], []
    for name in names:
        if name.strip().lower() in ("admins", "managers", "approvers"):
            people += [
                m.user for m in WorkspaceMembership.objects.filter(workspace=workspace, is_active=True).select_related("user")
                if m.can_approve_agent_runs
            ]
            continue
        person = _resolve_assignee(workspace, name)
        (people.append(person) if person else unknown.append(name))
    return list({p.id: p for p in people}.values()), unknown


def _find_metric(workspace, metric_name: str):
    from dashboard.models import GapAnalysisMetric

    return GapAnalysisMetric.objects.filter(workspace=workspace, metric=metric_name).order_by("-id").first()


def validate(workspace, trigger: str, frequency: str, action: str, config: Dict[str, Any]) -> List[str]:
    from dashboard.models import GapAnalysisMetric
    from .agent_runtime import AGENT_DIRECTORY

    errors = []
    if trigger not in dict(Automation.TRIGGER_CHOICES):
        errors.append(f"trigger must be one of: {', '.join(dict(Automation.TRIGGER_CHOICES))}")
    if frequency not in INTERVALS:
        errors.append(f"frequency must be one of: {', '.join(INTERVALS)}")
    if action not in dict(Automation.ACTION_CHOICES):
        errors.append(f"action must be one of: {', '.join(dict(Automation.ACTION_CHOICES))}")
    if trigger == "tasks_overdue" and action != "notify":
        errors.append("a tasks_overdue automation can only notify people")
    if trigger == "metric_off_target":
        metric = config.get("metric", "")
        if metric not in dict(GapAnalysisMetric.METRIC_CHOICES):
            errors.append("metric_off_target needs `metric`, one of the Gap Analysis metrics")
        elif not _find_metric(workspace, metric):
            errors.append(f"there's no '{metric}' gap in this workspace yet -- flag it first")
    if action == "create_task":
        if not str(config.get("note", "")).strip():
            errors.append("create_task needs `note`, the task to create")
        assignee = str(config.get("assignee", "")).strip()
        agent_names = {info["name"].lower() for info in AGENT_DIRECTORY.values()}
        if assignee and assignee.lower() not in agent_names and _resolve_people(workspace, [assignee])[1]:
            errors.append(f"no teammate or agent matches assignee '{assignee}'")
    if action == "notify":
        recipients = config.get("recipients") or []
        if not recipients:
            errors.append("notify needs `recipients` (teammate names, or 'admins')")
        else:
            unknown = _resolve_people(workspace, recipients)[1]
            if unknown:
                errors.append(f"no teammate matches: {', '.join(unknown)}")
        if not str(config.get("message", "")).strip():
            errors.append("notify needs `message`")
    return errors


def create_automation(
    *, workspace, name: str, trigger: str, frequency: str, action: str, config: Dict[str, Any],
    description: str = "", session=None, created_by=None, created_by_run: Optional[AgentRun] = None,
    approved_by=None, agent_type: str = "",
) -> Automation:
    errors = validate(workspace, trigger, frequency, action, config)
    if errors:
        raise AutomationError("; ".join(errors))
    return Automation.objects.create(
        workspace=workspace, session=session, name=name.strip()[:120] or "Automation",
        description=description.strip()[:2000], trigger=trigger, frequency=frequency, action=action, config=config,
        created_by=created_by, created_by_run=created_by_run, approved_by=approved_by, agent_type=agent_type,
        next_run_at=timezone.now() + INTERVALS[frequency],
    )


# ================================================================
# Running
# ================================================================

def _condition(automation: Automation, today) -> Tuple[bool, str]:
    """(should act, context sentence)."""
    if automation.trigger == "metric_off_target":
        from dashboard.views.helpers import _gap_percent_value

        metric = _find_metric(automation.workspace, automation.config.get("metric", ""))
        if metric is None:
            return False, "The gap metric no longer exists."
        if metric.status == "closed":
            return False, f"{metric.metric} is closed."
        if _gap_percent_value(metric) >= 0:
            return False, f"{metric.metric} is on target ({metric.current} vs {metric.target})."
        return True, f"{metric.metric} is {metric.current}, target {metric.target}."
    if automation.trigger == "tasks_overdue":
        overdue = list(
            ActionItem.objects.filter(workspace=automation.workspace, due_date__lt=today)
            .exclude(status="done").order_by("due_date")[:10]
        )
        if not overdue:
            return False, "No overdue tasks."
        listed = "; ".join(f"{t.note[:60]} (due {t.due_date:%b %d})" for t in overdue)
        return True, f"{len(overdue)} overdue task(s): {listed}"
    return True, ""


def _act(automation: Automation, context: str, today) -> Tuple[str, List[Dict[str, Any]]]:
    from dashboard.utils_notifications import send_notification
    from .agent_runtime import AGENT_DIRECTORY

    config = automation.config
    link = f"/dashboard/tasks/?workspace={automation.workspace_id}"
    if automation.action == "create_task":
        # Dated, so each period gets its own task instead of deduping into the first one.
        note = f"{config['note'].strip()} ({today:%b %d})"[:240]
        extra: Dict[str, Any] = {"created_by": automation.created_by}
        if config.get("due_in_days"):
            extra["due_date"] = today + timedelta(days=int(config["due_in_days"]))
        assignee = str(config.get("assignee", "")).strip()
        person = None
        if assignee:
            agent_type = next((t for t, i in AGENT_DIRECTORY.items() if i["name"].lower() == assignee.lower()), None)
            if agent_type:
                extra["assigned_agent_type"] = agent_type
            else:
                person = (_resolve_people(automation.workspace, [assignee])[0] or [None])[0]
                if person:
                    extra["assigned_to"] = person
        item, created = ActionItem.objects.create_deduped(
            note=note, session=automation.session, workspace=automation.workspace, **extra,
        )
        if not created:
            return f"Task already exists: {note}", []
        if person:
            send_notification(
                recipient=person, workspace=automation.workspace, notification_type="task", level="info",
                title=f"New task from '{automation.name}'", message=f"{note}. {context}".strip(), link=link,
            )
        return f"Created task: {note}. {context}".strip(), [{"task_id": item.id}]

    people, _ = _resolve_people(automation.workspace, config.get("recipients") or [])
    message = f"{config.get('message', '').strip()} {context}".strip()
    for person in people:
        send_notification(
            recipient=person, workspace=automation.workspace, notification_type="system", level="info",
            title=automation.name, message=message[:1000], link=link,
        )
    return f"Notified {len(people)} people. {context}".strip(), [{"user_id": p.id} for p in people]


def run_automation(automation: Automation, now=None) -> Optional[AutomationRun]:
    """Evaluate one automation once, if it's due and no other job has it."""
    from .ai_services import _acquire_lock, _release_lock

    now = now or timezone.now()
    key = automation_lock_key(automation.id)
    if not _acquire_lock(key, ttl_seconds=AUTOMATION_LOCK_TTL_SECONDS):
        return None
    try:
        automation.refresh_from_db()
        if not automation.is_active or automation.next_run_at > now:
            return None
        # Claim this period first, so a crash mid-action never repeats it.
        next_run = automation.next_run_at
        while next_run <= now:
            next_run += INTERVALS[automation.frequency]
        automation.last_run_at, automation.next_run_at = now, next_run
        automation.save(update_fields=["last_run_at", "next_run_at"])

        today = timezone.localdate(now)
        try:
            should_act, context = _condition(automation, today)
            if should_act:
                detail, refs = _act(automation, context, today)
                outcome = "acted"
            else:
                outcome, detail, refs = "skipped", context, []
        except Exception as e:
            logger.exception("Automation %s failed", automation.id)
            outcome, detail, refs = "failed", f"Error: {e}", []
        return AutomationRun.objects.create(automation=automation, outcome=outcome, detail=detail[:500], refs=refs)
    finally:
        _release_lock(key)


def run_due_automations(now=None) -> Dict[str, int]:
    now = now or timezone.now()
    counts = {"acted": 0, "skipped": 0, "failed": 0}
    due = Automation.objects.filter(is_active=True, next_run_at__lte=now).select_related("workspace", "session")
    for automation in due:
        run = run_automation(automation, now=now)
        if run is not None:
            counts[run.outcome] += 1
    return counts


def sweep_stale_runs(now=None) -> int:
    """Fail agent runs whose worker died, so the card offers a retry."""
    from .agent_runs import _fail, _set_item_ai_state, run_lock_key
    from .ai_services import _lock_is_held

    now = now or timezone.now()
    count = 0
    stale = AgentRun.objects.filter(status__in=AgentRun.ACTIVE_STATUSES, updated_at__lt=now - STALE_RUN_AFTER)
    for run in stale:
        if _lock_is_held(run_lock_key(run)):
            continue
        _fail(run, "This run stopped responding. You can retry it.")
        _set_item_ai_state(run, "failed", run.error)
        count += 1
    return count


# ================================================================
# Agent tools
# ================================================================

def describe(automation: Automation) -> str:
    when = {
        "schedule": f"every {automation.frequency.removesuffix('ly') if automation.frequency != 'daily' else 'day'}",
        "metric_off_target": f"{automation.frequency}, while {automation.config.get('metric')} is off target",
        "tasks_overdue": f"{automation.frequency}, when tasks are overdue",
    }[automation.trigger]
    what = (
        f"create the task \"{automation.config.get('note', '')}\""
        if automation.action == "create_task"
        else f"notify {', '.join(automation.config.get('recipients') or [])}"
    )
    return f"{when}: {what}"


def build_automation_read_tool(workspace):
    def list_automations() -> str:
        """List this workspace's automations (rules that keep running on a
        schedule or when a metric slips): what each does, whether it's
        active, and when it last ran. Check this before proposing a new
        automation so you don't duplicate one.
        """
        automations = list(Automation.objects.filter(workspace=workspace)[:20])
        if not automations:
            return "This workspace has no automations yet."
        lines = ["Automations:"]
        for a in automations:
            state = "active" if a.is_active else "paused"
            last = f", last ran {a.last_run_at:%b %d}" if a.last_run_at else ""
            lines.append(f"- {a.name} ({state}{last}): {describe(a)}")
        return "\n".join(lines)

    return list_automations
