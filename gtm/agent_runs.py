"""Agent runs: real, multi-step agent work with a human in the loop.

An agent never acts on its own say-so. A run goes:

1. planning -- the agent investigates with read-only tools, then calls
   propose_plan with concrete steps drawn from STEP_KINDS.
2. awaiting_approval -- a person reviews the plan, can untick steps, and
   approves it (or rejects it, optionally with feedback to re-plan).
3. running -- the agent carries the plan out. Every write tool is wrapped by
   a gate that refuses anything the approved plan doesn't contain, and a
   step only counts as done when the tool reports a real object it created
   or changed (see agent_actions._record_outcome) -- never on the model's
   own account.
4. waiting_for_input -- the agent asked a teammate something; the run
   pauses until they answer, then resumes from the plan's recorded state
   (not from Gemini chat history, so a restart loses nothing).
5. completed / failed / rejected / cancelled.

Background work is plain threads (run_in_background) guarded by a DB lock,
the same pattern as every other AI job in this app.
"""

import functools
import logging
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel

from .data_tools import DataError, DataFilter, compute, find_document
from .models import ActionItem, ActionItemComment, AgentDocument, AgentRun, AgentRunQuestion
from .utils_async import run_in_background
from .utils_logging import log_ai_error

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

MODEL = "gemini-2.5-flash"
MAX_PLAN_STEPS = 8
PLANNING_TOOL_ROUNDS = 12
EXECUTION_TOOL_ROUNDS = 20
# Follow-up turns when steps are still pending after a turn ends.
MAX_EXECUTION_TURNS = 3
# Long enough for a full multi-step execution; past it the card offers a retry.
RUN_LOCK_TTL_SECONDS = 600

# kind -> (what the planner is told it means, the tools that perform it).
STEP_KINDS: Dict[str, tuple] = {
    "create_document": (
        "Write or update a concrete deliverable (policy, spec, checklist, brief, playbook) with real content",
        ("create_document", "edit_document"),
    ),
    "create_subtasks": (
        "Break the work into follow-up tasks, each with an owner (teammate or agent) and a due date",
        ("create_subtasks",),
    ),
    "assign_task": ("Assign an existing task to a teammate, or route it to another agent", ("assign_task", "assign_task_to_agent")),
    "ask_teammate": (
        "Ask a teammate for information you can't find yourself, and wait for their answer",
        ("ask_teammate",),
    ),
    "notify_teammate": ("Send a teammate a notification", ("notify_teammate",)),
    "comment": ("Comment on a task", ("comment_on_task",)),
    "recommend_resource": ("Recommend a Library resource for this assessment", ("recommend_resource",)),
    "flag_gap": ("Flag a measurable GTM gap in Gap Analysis for the team to track", ("escalate_gap",)),
    "create_automation": (
        "Set up an automation that keeps running: on a schedule, or while a gap metric is off target, or when "
        "tasks are overdue, it creates a task or notifies people",
        ("create_automation",),
    ),
    "propose_measurement": (
        "Compute a gap metric's current value from an uploaded data file and propose it for a person to confirm",
        ("propose_measurement",),
    ),
    "crm_update": ("Write a note or task into the connected CRM", ("log_insight_to_crm", "create_crm_follow_up_task")),
}
TOOL_KIND = {tool: kind for kind, (_, tools) in STEP_KINDS.items() for tool in tools}

# Never available inside a run: no person said the number in chat
# (log_gap_measurement_from_chat), or the tool writes outside the gate.
EXCLUDED_TOOLS = {"log_gap_measurement_from_chat", "build_prioritized_action_plan", "draft_client_summary"}
EXCLUDED_PREFIXES = ("consult_", "transfer_to_")


class PlanStep(BaseModel):
    kind: str
    title: str
    detail: str = ""


class SubtaskSpec(BaseModel):
    note: str
    assignee: str = ""
    due_date: str = ""


# ================================================================
# Small helpers
# ================================================================

def run_lock_key(run: AgentRun) -> str:
    """One worker per run. A task's run shares the task's AI lock, so the
    board's existing stale-run detection (ai_run_display_state) keeps working."""
    if run.action_item_id:
        from dashboard.views.helpers import action_item_ai_lock_key

        return action_item_ai_lock_key(run.action_item_id)
    return f"gtm:ai:agent_run:{run.id}:lock"


def run_link(run: AgentRun) -> str:
    qs = f"?workspace={run.workspace_id}&run={run.id}" if run.workspace_id else f"?run={run.id}"
    return f"/dashboard/tasks/{qs}"


def _append_log(run: AgentRun, event: str, **data: Any) -> None:
    run.log = [*run.log, {"at": timezone.now().isoformat(), "event": event, **data}]


def _save(run: AgentRun, *fields: str) -> None:
    run.save(update_fields=[*fields, "updated_at"])


def _set_item_ai_state(run: AgentRun, state: str, error: str = "") -> None:
    if run.action_item_id:
        fields = {"ai_run_state": state, "ai_run_error": error[:240]}
        if state == "running":
            fields["ai_run_started_at"] = timezone.now()
        ActionItem.objects.filter(pk=run.action_item_id).update(**fields)


def _membership(user, workspace):
    from .models_workspace import WorkspaceMembership

    if not user or not workspace:
        return None
    return WorkspaceMembership.objects.filter(user=user, workspace=workspace, is_active=True).first()


def can_view_run(user, run: AgentRun) -> bool:
    if run.workspace_id:
        return _membership(user, run.workspace) is not None
    return bool(run.session and run.session.user_id == getattr(user, "id", None))


def can_review_run(user, run: AgentRun) -> bool:
    """Who may approve, reject, cancel, or retry: workspace members with
    can_approve_agent_runs, or the owner of a personal assessment."""
    if run.workspace_id:
        membership = _membership(user, run.workspace)
        return bool(membership and membership.can_approve_agent_runs)
    return bool(run.session and run.session.user_id == getattr(user, "id", None))


def can_answer_question(user, question: AgentRunQuestion) -> bool:
    return question.asked_to_id == getattr(user, "id", None) or can_review_run(user, question.run)


def _reviewers(run: AgentRun) -> List[Any]:
    """Who to ask for approval: the requester if they can approve, else the
    workspace's approvers."""
    from .models_workspace import WorkspaceMembership

    if run.requested_by and can_review_run(run.requested_by, run):
        return [run.requested_by]
    if not run.workspace_id:
        return [run.session.user] if run.session and run.session.user else []
    return [
        m.user for m in WorkspaceMembership.objects.filter(workspace=run.workspace, is_active=True).select_related("user")
        if m.can_approve_agent_runs
    ]


def _notify(recipients, run: AgentRun, notification_type: str, title: str, message: str, level: str = "info") -> None:
    from dashboard.utils_notifications import send_notification

    for recipient in {r for r in recipients if r}:
        send_notification(
            recipient=recipient, sender=run.requested_by, workspace=run.workspace,
            notification_type=notification_type, level=level, title=title, message=message, link=run_link(run),
        )


def _agent_info(agent_type: str) -> Dict[str, str]:
    from .agent_runtime import AGENT_DIRECTORY

    return AGENT_DIRECTORY[agent_type]


# ================================================================
# Starting and relaunching
# ================================================================

def start_run(
    *, agent_type: str, goal: str, user, session=None, workspace=None, action_item: Optional[ActionItem] = None,
) -> Optional[AgentRun]:
    """Create a run and start its planning turn in the background. Returns
    None if a worker already holds this task's lock."""
    from .agent_runtime import AGENT_DIRECTORY

    if agent_type not in AGENT_DIRECTORY:
        raise ValueError(f"Unknown agent_type: {agent_type}")
    if action_item is not None:
        session = session or action_item.session
        workspace = workspace or action_item.workspace
    workspace = workspace or (session.workspace if session else None)
    run = AgentRun.objects.create(
        agent_type=agent_type, goal=goal[:4000], requested_by=user, session=session,
        workspace=workspace, action_item=action_item, status="planning",
    )
    if action_item is not None:
        ActionItem.objects.filter(pk=action_item.pk).update(current_run=run)
    if not launch(run, "plan"):
        run.status, run.error = "cancelled", "Another run is already working on this task."
        _save(run, "status", "error")
        return None
    return run


def launch(run: AgentRun, phase: str) -> bool:
    """Run `phase` ("plan" or "execute") for `run` in the background.
    False if a worker already holds the run's lock."""
    from .ai_services import _acquire_lock

    if not _acquire_lock(run_lock_key(run), ttl_seconds=RUN_LOCK_TTL_SECONDS):
        return False
    _set_item_ai_state(run, "running")
    run_in_background(_worker, run.id, phase, name=f"agent_run_{phase}:{run.id}")
    return True


def _worker(run_id, phase: str) -> None:
    from .ai_services import _release_lock

    run = AgentRun.objects.select_related("session", "workspace", "action_item", "requested_by").get(pk=run_id)
    try:
        if phase == "plan":
            _plan(run)
        else:
            _execute(run)
    except Exception as e:
        log_ai_error("Agent run failed", e, service="google-genai", model=MODEL, extra={"run_id": str(run_id), "phase": phase})
        run.refresh_from_db()
        if run.status in AgentRun.ACTIVE_STATUSES:
            _fail(run, "Ran into a technical error. You can retry this run.")
    finally:
        run.refresh_from_db()
        if run.status == "failed":
            _set_item_ai_state(run, "failed", run.error or "The run failed.")
        else:
            _set_item_ai_state(run, "done")
        _release_lock(run_lock_key(run))


def _fail(run: AgentRun, error: str) -> None:
    run.status, run.error, run.finished_at = "failed", error[:240], timezone.now()
    _append_log(run, "failed", error=error)
    _save(run, "status", "error", "finished_at", "log")


# ================================================================
# Tools
# ================================================================

def _is_excluded(name: str) -> bool:
    return name in EXCLUDED_TOOLS or name.startswith(EXCLUDED_PREFIXES)


def _available_kinds(run: AgentRun, tools: List[Any]) -> List[str]:
    """Step kinds the planner may use: those whose tools exist for this run,
    minus CRM writes when no CRM is connected (the step could never succeed)."""
    from .integrations.hubspot_client import get_client_for_workspace

    kinds = [k for k in STEP_KINDS if any(TOOL_KIND.get(t.__name__) == k for t in tools)]
    if "crm_update" in kinds and not get_client_for_workspace(run.workspace):
        kinds.remove("crm_update")
    return kinds


def _build_run_tools(run: AgentRun, state: Dict[str, Any]) -> List[Any]:
    """Every tool the run's agent can use, unwrapped. `state` collects
    outcomes/created documents so the gate can tell what really happened."""
    from .action_item_completion import build_task_tools
    from .agent_actions import build_agent_action_tools
    from .web_tools import build_web_tools

    user = run.requested_by
    tools: List[Any] = []
    if run.session:
        tools += build_task_tools(run.session, user, state["doc_ids"], agent_type=run.agent_type, workspace=run.workspace)
        tools += build_web_tools(session=run.session, user=user)
    if run.workspace and user:
        tools += build_agent_action_tools(
            run.workspace, user, task_refs_sink=state["task_refs"], agent_type=run.agent_type,
            outcomes_sink=state["outcomes"],
        )
    tools += _build_run_write_tools(run, state)
    if run.workspace:
        from .automations import build_automation_read_tool

        tools.append(build_automation_read_tool(run.workspace))
    # Builders can overlap (e.g. a workspace agent's domain tools); Gemini
    # rejects duplicate tool names, so the first one wins.
    unique: Dict[str, Any] = {}
    for t in tools:
        if not _is_excluded(t.__name__):
            unique.setdefault(t.__name__, t)
    return list(unique.values())


def _build_run_write_tools(run: AgentRun, state: Dict[str, Any]) -> List[Any]:
    """Write tools that exist only inside runs."""
    from dashboard.parsers import _resolve_assignee
    from django.utils.dateparse import parse_date
    from .agent_actions import _record_outcome
    from .agent_runtime import AGENT_DIRECTORY

    user = run.requested_by
    outcomes = state["outcomes"]

    def _resolve_person(name: str):
        candidate = (name or "").strip()
        if not candidate:
            return None
        if user and candidate.lower() in {"me", "myself", user.username.lower(), (user.get_full_name() or "").lower()}:
            return user
        return _resolve_assignee(run.workspace, candidate) if run.workspace else None

    def create_subtasks(tasks: list[SubtaskSpec]) -> str:
        """Create follow-up tasks on the Tasks board, linked to the task this
        run is working on. Each has a `note` (the concrete task, specific to
        this company), an optional `assignee` (a teammate's name, username,
        or email, or a fellow agent's name such as Theo), and an optional
        `due_date` (YYYY-MM-DD). Give every task an owner and a date unless
        you truly can't.
        """
        parent = run.action_item
        created, existing, problems = [], [], []
        for spec in tasks:
            spec = spec if isinstance(spec, SubtaskSpec) else SubtaskSpec(**spec)
            note = spec.note.strip()[:240]
            if not note:
                continue
            extra: Dict[str, Any] = {"created_by": user, "parent": parent}
            if parent is not None and parent.gap_metric_id:
                extra["gap_metric_id"] = parent.gap_metric_id
            if spec.due_date.strip():
                due = parse_date(spec.due_date.strip())
                if due:
                    extra["due_date"] = due
                else:
                    problems.append(f"'{spec.due_date}' isn't a YYYY-MM-DD date (used no date for \"{note[:40]}\")")
            if spec.assignee.strip():
                agent_type = next(
                    (t for t, info in AGENT_DIRECTORY.items() if info["name"].lower() == spec.assignee.strip().lower()),
                    None,
                )
                person = None if agent_type else _resolve_person(spec.assignee)
                if agent_type:
                    extra["assigned_agent_type"] = agent_type
                elif person:
                    extra["assigned_to"] = person
                else:
                    problems.append(f"no teammate matches '{spec.assignee}' (left \"{note[:40]}\" unassigned)")
            item, was_created = ActionItem.objects.create_deduped(
                note=note, session=run.session, workspace=run.workspace, **extra,
            )
            if item is None:
                continue
            (created if was_created else existing).append(item)
        if not created and not existing:
            return "No tasks were created -- every task needs a note."
        for item in created:
            _record_outcome(outcomes, "create_subtasks", task_id=item.id)
        lines = [f'Created {len(created)} task(s): ' + "; ".join(f'"{i.note[:60]}"' for i in created)] if created else []
        if existing:
            lines.append("Already on the board: " + "; ".join(f'"{i.note[:60]}"' for i in existing))
        if problems:
            lines.append("Notes: " + "; ".join(problems))
        return "\n".join(lines)

    def ask_teammate(teammate: str, question: str) -> str:
        """Ask a teammate (name, username, or email -- or "me" for the person
        who requested this work) a specific question you can't answer from
        your tools, and pause this run until they reply. Ask only for what
        you actually need, in one clear question. After calling this, stop
        and end your turn; you'll resume with their answer.
        """
        person = _resolve_person(teammate)
        if not person:
            return f"I couldn't find a teammate matching '{teammate}'. Use list_teammates to see who's in this workspace."
        AgentRunQuestion.objects.create(run=run, asked_to=person, question=question.strip()[:2000])
        _record_outcome(outcomes, "ask_teammate", user_id=person.id)
        state["paused"] = True
        _notify(
            [person], run, "agent_question",
            f"{run.agent_name} has a question",
            f"{run.agent_name} needs your input to continue: {question.strip()[:200]}",
        )
        return (
            f"Asked {person.get_full_name() or person.username}. This run now waits for their answer -- "
            "stop here and end your turn with a one-sentence note of what you're waiting for."
        )

    def propose_measurement(
        metric_name: str, file_id: str, operation: str, column: str = "",
        filters: Optional[list[DataFilter]] = None, denominator_filters: Optional[list[DataFilter]] = None,
    ) -> str:
        """Compute a gap metric's current value from an uploaded data file
        and propose it as a measurement. You don't supply the number: this
        tool computes it from the file with the same operation/column/filter
        arguments as compute_metric_from_file (try those first to get them
        right). A person confirms the value before it's recorded, so it can
        never move or close a gap on its own. `metric_name` must be one of:
        Monthly Qualified Leads, Average Deal Size, Net Revenue Retention,
        Product Qualified Leads, Win Rate, CAC Payback Period, and the gap
        must already exist (use a flag_gap step first if it doesn't).
        """
        from dashboard.models import GapAnalysisMetric, GapMeasurementProposal
        from dashboard.views.helpers import _gap_metric_scope_queryset

        if metric_name not in dict(GapAnalysisMetric.METRIC_CHOICES):
            return f"'{metric_name}' isn't a recognized metric."
        metric = _gap_metric_scope_queryset(user, run.workspace, metric_name).first()
        if not metric:
            return f"There's no '{metric_name}' gap to measure yet -- it has to be flagged first."
        try:
            doc = find_document(file_id, session=run.session, workspace=run.workspace)
            result = compute(doc, operation, column, filters, denominator_filters)
        except DataError as e:
            return str(e)
        proposal = GapMeasurementProposal.objects.create(
            gap_metric=metric, value=result["value"], method=result["description"][:2000],
            source_file_name=doc.original_filename[:255], source_file_id=str(doc.pk), run=run,
            agent_type=run.agent_type,
        )
        _record_outcome(outcomes, "propose_measurement", proposal_id=proposal.id)
        _notify(
            _reviewers(run), run, "agent_plan",
            f"{run.agent_name} measured {metric.metric}",
            f"{run.agent_name} computed {metric.metric} = {result['value']:g} from {doc.original_filename}. Confirm it to record it.",
        )
        return (
            f"Proposed {metric.metric} = {result['value']:g} ({result['description']}). "
            "A person must confirm it before it counts."
        )

    def create_automation(
        name: str, trigger: str, action: str, frequency: str = "weekly", description: str = "",
        metric_name: str = "", task_note: str = "", assignee: str = "", due_in_days: int = 0,
        recipients: Optional[list[str]] = None, message: str = "",
    ) -> str:
        """Set up a rule that keeps running after this run ends, exactly as
        the approved plan described. `trigger`: schedule (every period),
        metric_off_target (each period while the gap `metric_name` is off
        target), or tasks_overdue (each period when tasks are overdue).
        `frequency`: daily, weekly, or monthly. `action`: create_task (needs
        `task_note`; optional `assignee` -- teammate or agent name -- and
        `due_in_days`) or notify (needs `recipients` -- teammate names, or
        "admins" -- and `message`). tasks_overdue can only notify.
        """
        from .automations import AutomationError, create_automation as create_rule, describe

        config: Dict[str, Any] = {}
        if trigger == "metric_off_target":
            config["metric"] = metric_name
        if action == "create_task":
            config.update(note=task_note, assignee=assignee, due_in_days=max(0, int(due_in_days or 0)))
        elif action == "notify":
            config.update(recipients=list(recipients or []), message=message)
        try:
            automation = create_rule(
                workspace=run.workspace, session=run.session, name=name, description=description,
                trigger=trigger, frequency=frequency, action=action, config=config, created_by=user,
                created_by_run=run, approved_by=run.approved_by, agent_type=run.agent_type,
            )
        except AutomationError as e:
            return f"Couldn't create the automation: {e}"
        _record_outcome(outcomes, "create_automation", automation_id=str(automation.id))
        _notify(
            _reviewers(run), run, "agent_plan", f"{run.agent_name} set up an automation",
            f"'{automation.name}' will run {describe(automation)}. Pause or delete it any time from Automations on the Tasks board.",
        )
        return f"Created the automation '{automation.name}': {describe(automation)}. First run {automation.next_run_at:%b %d}."

    tools = [create_subtasks, ask_teammate, propose_measurement]
    if run.workspace:
        tools.append(create_automation)
    return tools


def _gate(run: AgentRun, tool, state: Dict[str, Any]):
    """Wrap a write tool so it only runs as part of a pending approved step
    of its kind, and marks that step done only when a real outcome shows up."""
    kind = TOOL_KIND[tool.__name__]

    @functools.wraps(tool)
    def gated(*args, **kwargs):
        run.refresh_from_db(fields=["status", "plan", "log"])
        if run.status != "running":
            return "This run is no longer running (it was cancelled or finished). Stop now."
        if state["paused"]:
            return "You're waiting for a teammate's answer. Stop now and end your turn."
        step = next((s for s in run.plan if s["kind"] == kind and s["status"] in ("pending", "failed")), None)
        if step is None:
            remaining = [f'{s["id"]} ({s["kind"]}): {s["title"]}' for s in run.plan if s["status"] in ("pending", "failed")]
            _append_log(run, "refused", tool=tool.__name__, reason="not in approved plan")
            _save(run, "log")
            return (
                f"Refused: the approved plan has no remaining '{kind}' step, and you may only do what was approved. "
                + ("Remaining steps: " + "; ".join(remaining) if remaining else "Every approved step is done -- stop now.")
            )

        before_outcomes, before_docs = len(state["outcomes"]), len(state["doc_ids"])
        try:
            result = tool(*args, **kwargs)
        except Exception as e:
            logger.exception("Gated tool %s failed in run %s", tool.__name__, run.id)
            result = f"That failed with an error: {e}"
        refs = [dict(o) for o in state["outcomes"][before_outcomes:]]
        refs += [{"tool": tool.__name__, "document_id": str(d)} for d in state["doc_ids"][before_docs:]]

        run.refresh_from_db(fields=["plan", "log"])
        for s in run.plan:
            if s["id"] == step["id"]:
                if refs:
                    s["status"], s["refs"] = "done", [*s.get("refs", []), *refs]
                s["result"] = str(result)[:500]
        _append_log(run, "tool", tool=tool.__name__, step=step["id"], ok=bool(refs), result=str(result)[:300])
        _save(run, "plan", "log")
        return result

    return gated


# ================================================================
# Gemini turns
# ================================================================

def _client_or_error(run: AgentRun):
    """(client, account) or (None, error message), after quota and credit checks."""
    from .ai_chat import _get_chat_client
    from .ai_credits import can_spend, credits_exhausted_message, resolve_account_for_session
    from .ai_services import _quota_cooldown_active

    if _quota_cooldown_active():
        return None, None, "AI is cooling down to stay within API limits. Please retry shortly."
    if run.session:
        check = can_spend(account=resolve_account_for_session(run.session, user=run.requested_by))
    else:
        check = can_spend(workspace=run.workspace, user=run.requested_by)
    if not check.allowed:
        return None, None, credits_exhausted_message(check.reset_at)
    account = check.account
    client = _get_chat_client()
    if not client:
        return None, None, "Could not connect to the AI service. Please retry in a moment."
    return client, account, ""


def _send(run: AgentRun, chat, account, message: str):
    """One send_message with credit recording; quota errors set the cooldown
    and re-raise as a readable failure."""
    from .ai_credits import record_spend
    from .ai_services import _extract_retry_delay_seconds, _is_quota_error, _set_quota_cooldown

    try:
        response = chat.send_message(message)
    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            raise RunStopped("Hit a temporary AI quota. Please retry shortly.") from e
        raise
    usage = getattr(response, "usage_metadata", None)
    total = getattr(usage, "total_token_count", None) if usage else None
    if total:
        run.total_tokens += total
        _save(run, "total_tokens")
        record_spend(account, total, "agent_run", session=run.session, actor=run.requested_by)
    return response


class RunStopped(Exception):
    """A readable reason to fail the run (quota, credits)."""


def _persona(run: AgentRun) -> str:
    info = _agent_info(run.agent_type)
    company = (run.session.company_name if run.session else "") or (run.workspace.name if run.workspace else "") or "this company"
    return f"You are {info['name']}, the {info['label']}, working for {company}. Your specialty: {info['domain']}"


def _config(system_instruction: str, tools: List[Any], rounds: int, thinking_budget: int):
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.4,
        max_output_tokens=4096,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=rounds),
    )


def _plan(run: AgentRun) -> None:
    from .agent_runtime import build_task_context_prompt

    client, account, error = _client_or_error(run)
    if error:
        return _fail(run, error)

    state = {"outcomes": [], "doc_ids": [], "task_refs": [], "paused": False}
    all_tools = _build_run_tools(run, state)
    read_tools = [t for t in all_tools if t.__name__ not in TOOL_KIND]
    available_kinds = _available_kinds(run, all_tools)

    def propose_plan(summary: str, steps: list[PlanStep]) -> str:
        """Submit your plan for human approval. `summary` is one or two
        sentences on what the plan achieves. `steps` are 1-8 concrete steps
        in the order you'll do them, each with a `kind` (from the list in
        your instructions), a short `title`, and a `detail` saying exactly
        what you'll produce or do -- specific to this company. Nothing
        happens until a person approves; then you'll carry it out.
        """
        parsed = [s if isinstance(s, PlanStep) else PlanStep(**s) for s in steps]
        if not parsed:
            return "A plan needs at least one step."
        if len(parsed) > MAX_PLAN_STEPS:
            return f"Keep it to {MAX_PLAN_STEPS} steps or fewer -- combine or drop the least important ones."
        bad = sorted({s.kind for s in parsed if s.kind not in available_kinds})
        if bad:
            return f"Unknown or unavailable step kind(s): {', '.join(bad)}. Use only: {', '.join(available_kinds)}."
        run.refresh_from_db(fields=["status"])
        if run.status != "planning":
            return "This run is no longer planning. Stop now."
        run.plan = [
            {"id": f"s{i}", "kind": s.kind, "title": s.title.strip()[:200], "detail": s.detail.strip()[:1500],
             "status": "pending", "result": "", "refs": []}
            for i, s in enumerate(parsed, start=1)
        ]
        run.plan_summary = summary.strip()[:1000]
        run.status = "awaiting_approval"
        _append_log(run, "plan_proposed", steps=len(run.plan))
        _save(run, "plan", "plan_summary", "status", "log")
        return "Plan submitted for approval. Stop now and end your turn with one sentence."

    catalog = "\n".join(f"- {k}: {STEP_KINDS[k][0]}" for k in available_kinds)
    instruction = (
        f"{_persona(run)}\n\n"
        f"You've been asked to: {run.goal}\n\n"
        "First investigate with your tools -- assessment scores and the scoring-engine diagnosis, existing "
        "documents and tasks, uploaded evidence, the resource library, and anything else relevant. Then call "
        "propose_plan. A person approves the plan before anything happens; once approved you'll carry it out "
        "with real tools, and tools refuse anything that isn't in the approved plan.\n\n"
        "Plan the real work, not a description of it: the actual deliverable with its real contents, "
        "follow-up tasks with owners and due dates, gaps to track, the exact question to ask a teammate when "
        "something important is unknown (never guess or invent facts or numbers). Give work a real owner: "
        "check list_teammates and name the teammate (or a fellow agent) who should own each task, and a due "
        "date. Every step must be specific to this company and doable with one of these kinds:\n"
        f"{catalog}"
        + build_task_context_prompt()
    )
    if run.feedback:
        previous = "\n".join(f"- {s['kind']}: {s['title']} -- {s['detail']}" for s in run.plan) or "(none)"
        instruction += (
            f"\n\nA reviewer sent back your previous plan:\n{previous}\n\nTheir feedback: {run.feedback}\n"
            "Propose a revised plan that addresses it."
        )

    chat = client.chats.create(
        model=MODEL, config=_config(instruction, [*read_tools, propose_plan], PLANNING_TOOL_ROUNDS, 1024), history=[],
    )
    try:
        _send(run, chat, account, "Investigate, then propose your plan.")
        run.refresh_from_db()
        if run.status == "planning":
            _send(run, chat, account, "You haven't proposed a plan yet. Call propose_plan now.")
            run.refresh_from_db()
    except RunStopped as e:
        return _fail(run, str(e))

    if run.status == "planning":
        return _fail(run, f"{run.agent_name} didn't propose a plan. You can retry.")
    if run.status == "awaiting_approval":
        _notify(
            _reviewers(run), run, "agent_plan",
            f"{run.agent_name} proposed a plan",
            f"{run.agent_name} proposed a {len(run.plan)}-step plan for: {run.goal[:120]}. Review it to continue.",
        )


def _execution_brief(run: AgentRun) -> str:
    lines = []
    for s in run.plan:
        if s["status"] == "skipped":
            continue
        mark = "DONE" if s["status"] == "done" else "TO DO"
        lines.append(f"[{mark}] {s['id']} ({s['kind']}): {s['title']} -- {s['detail']}")
    answers = [
        f"Q to {q.asked_to.get_full_name() or q.asked_to.username}: {q.question}\nA: {q.answer or '(not answered yet)'}"
        for q in run.questions.select_related("asked_to")
    ]
    brief = "Approved plan (do the TO DO steps, in order):\n" + "\n".join(lines)
    if answers:
        brief += "\n\nTeammate answers so far:\n" + "\n\n".join(answers)
    return brief


def _pending_steps(run: AgentRun) -> List[dict]:
    return [s for s in run.plan if s["status"] in ("pending", "failed")]


def _execute(run: AgentRun) -> None:
    from .agent_runtime import build_task_context_prompt

    if run.status != "running":
        return
    client, account, error = _client_or_error(run)
    if error:
        return _fail(run, error)

    state = {"outcomes": [], "doc_ids": [], "task_refs": [], "paused": False}
    tools = [_gate(run, t, state) if t.__name__ in TOOL_KIND else t for t in _build_run_tools(run, state)]
    instruction = (
        f"{_persona(run)}\n\n"
        f"A person approved your plan for: {run.goal}\n\n"
        f"{_execution_brief(run)}\n\n"
        "Carry out each TO DO step now with your tools, in order, doing the real work: write the full "
        "deliverable (not a template), create the tasks with owners and dates, and so on. Use your read tools "
        "whenever you need facts; never invent numbers. You can only do what the plan approved -- other writes "
        "are refused. If you truly need information only a teammate has, use ask_teammate (if it's in the "
        "plan) and stop. When done, reply with a 2-4 sentence summary of exactly what you did."
        + build_task_context_prompt()
    )
    chat = client.chats.create(model=MODEL, config=_config(instruction, tools, EXECUTION_TOOL_ROUNDS, 0), history=[])

    message = "Carry out the approved plan."
    last_text = ""
    try:
        for _ in range(MAX_EXECUTION_TURNS):
            response = _send(run, chat, account, message)
            last_text = (response.text or "").strip() or last_text
            run.refresh_from_db()
            if run.status != "running" or state["paused"] or not _pending_steps(run):
                break
            still = "; ".join(f"{s['id']} ({s['kind']}): {s['title']}" for s in _pending_steps(run))
            message = f"These approved steps aren't done yet: {still}. Do them now with your tools."
    except RunStopped as e:
        return _fail(run, str(e))

    run.refresh_from_db()
    if run.status != "running":
        return
    if state["paused"]:
        run.status = "waiting_for_input"
        _append_log(run, "waiting_for_input")
        _save(run, "status", "log")
        return
    pending = _pending_steps(run)
    if pending:
        for s in run.plan:
            if s in pending:
                s["status"] = "failed"
        run.plan = list(run.plan)
        run.result_summary = last_text[:2000]
        _save(run, "plan", "result_summary")
        _fail(run, f"{len(pending)} approved step(s) didn't get done. You can retry them.")
        _write_back_to_task(run)
        return
    run.status, run.finished_at, run.result_summary = "completed", timezone.now(), last_text[:2000]
    _append_log(run, "completed")
    _save(run, "status", "finished_at", "result_summary", "log")
    _write_back_to_task(run)


def _write_back_to_task(run: AgentRun) -> None:
    """Honest task write-back: Done only when every approved step really
    happened; otherwise In progress with what's left."""
    from dashboard.parsers import log_workspace_activity

    item = run.action_item
    if item is None:
        if run.status == "completed":
            _notify([run.requested_by], run, "task_status", f"{run.agent_name} finished", run.result_summary[:200] or run.goal[:200], "success")
        return
    item.refresh_from_db()
    done_steps = [s for s in run.plan if s["status"] == "done"]
    doc_ids = [r["document_id"] for s in done_steps for r in s.get("refs", []) if r.get("document_id")]
    if run.status == "completed":
        item.status = "done"
        update = ["status", "updated_at"]
        if doc_ids:
            item.deliverable_document = AgentDocument.objects.filter(pk__in=doc_ids).order_by("-updated_at").first()
            update.append("deliverable_document")
        item.save(update_fields=update)
        text = f"{run.agent_name} completed this task: {run.result_summary or run.plan_summary}"
        log_workspace_activity(
            run.workspace, run.requested_by, "task_moved", f"{run.agent_name} completed task '{item.note[:80]}'.",
            object_type="action_item", object_id=item.id, metadata={"status": "done", "ai_completed": True},
            session=run.session,
        )
        recipients = [r for r in (item.created_by, item.assigned_to) if r and r != run.requested_by]
        _notify(recipients, run, "task_status", f"Task completed by {run.agent_name}", f"{run.agent_name} completed: {item.note[:80]}", "success")
    else:
        left = "; ".join(s["title"] for s in run.plan if s["status"] == "failed")
        text = f"{run.agent_name} did {len(done_steps)} of {len(run.active_steps)} approved steps. Not done yet: {left}"
        if item.status != "doing":
            item.status = "doing"
            item.save(update_fields=["status", "updated_at"])
    ActionItemComment.objects.create(
        action_item=item, user=run.requested_by, text=text[:4000], agent_type=run.agent_type,
    )


# ================================================================
# Human decisions
# ================================================================

class RunDecisionError(Exception):
    """A human decision that can't be applied (wrong state, no permission)."""


def approve_run(run: AgentRun, user, keep_step_ids: List[str]) -> None:
    """Approve the plan with only `keep_step_ids` (others are skipped), then
    start executing."""
    if not can_review_run(user, run):
        raise RunDecisionError("You don't have permission to approve agent plans here.")
    with transaction.atomic():
        run = AgentRun.objects.select_for_update().get(pk=run.pk)
        if run.status != "awaiting_approval":
            raise RunDecisionError("This plan isn't awaiting approval anymore.")
        keep = set(keep_step_ids)
        if not keep & {s["id"] for s in run.plan}:
            raise RunDecisionError("Keep at least one step, or reject the plan instead.")
        for s in run.plan:
            if s["id"] not in keep:
                s["status"] = "skipped"
        run.status, run.approved_by, run.approved_at = "running", user, timezone.now()
        _append_log(run, "approved", by=user.username, skipped=[s["id"] for s in run.plan if s["status"] == "skipped"])
        _save(run, "plan", "status", "approved_by", "approved_at", "log")
    if run.action_item_id:
        ActionItem.objects.filter(pk=run.action_item_id, status="todo").update(status="doing", updated_at=timezone.now())
    if not launch(run, "execute"):
        raise RunDecisionError("This run is already being worked on.")


def reject_run(run: AgentRun, user, feedback: str = "", revise: bool = False) -> None:
    """Reject the plan. With `revise`, the agent re-plans using `feedback`."""
    if not can_review_run(user, run):
        raise RunDecisionError("You don't have permission to review agent plans here.")
    if run.status != "awaiting_approval":
        raise RunDecisionError("This plan isn't awaiting approval anymore.")
    feedback = (feedback or "").strip()
    if revise and not feedback:
        raise RunDecisionError("Say what should change so the agent can revise the plan.")
    run.feedback = feedback[:2000]
    _append_log(run, "rejected", by=user.username, feedback=feedback, revise=revise)
    if revise:
        run.status = "planning"
        _save(run, "feedback", "status", "log")
        if not launch(run, "plan"):
            raise RunDecisionError("This run is already being worked on.")
        return
    run.status, run.finished_at = "rejected", timezone.now()
    _save(run, "feedback", "status", "finished_at", "log")
    _set_item_ai_state(run, "")


def answer_question(question: AgentRunQuestion, user, answer: str) -> None:
    """Record an answer; once nothing is left open, the run resumes."""
    if not can_answer_question(user, question):
        raise RunDecisionError("This question was asked to someone else.")
    answer = (answer or "").strip()
    if not answer:
        raise RunDecisionError("Write an answer first.")
    if not question.is_open:
        raise RunDecisionError("This question has already been answered.")
    question.answer, question.answered_by, question.answered_at = answer[:4000], user, timezone.now()
    question.save(update_fields=["answer", "answered_by", "answered_at"])
    run = question.run
    _append_log(run, "answered", by=user.username)
    _save(run, "log")
    if run.status == "waiting_for_input" and not run.questions.filter(answered_at__isnull=True).exists():
        run.status = "running"
        _save(run, "status")
        if not launch(run, "execute"):
            raise RunDecisionError("This run is already being worked on.")


def cancel_run(run: AgentRun, user) -> None:
    if not can_review_run(user, run):
        raise RunDecisionError("You don't have permission to cancel this run.")
    if run.status in ("completed", "rejected", "cancelled"):
        raise RunDecisionError("This run has already finished.")
    run.status, run.finished_at = "cancelled", timezone.now()
    _append_log(run, "cancelled", by=user.username)
    _save(run, "status", "finished_at", "log")
    _set_item_ai_state(run, "")


def retry_run(run: AgentRun, user) -> None:
    """Retry a failed or stalled run from where it stopped: re-plan if it
    never got an approved plan, else execute the steps still not done."""
    if not can_review_run(user, run):
        raise RunDecisionError("You don't have permission to retry this run.")
    from .ai_services import _lock_is_held

    if run.status not in ("failed", "planning", "running"):
        raise RunDecisionError("Only a failed or stalled run can be retried.")
    if run.status in AgentRun.ACTIVE_STATUSES and _lock_is_held(run_lock_key(run)):
        raise RunDecisionError("This run is still working.")
    phase = "execute" if run.approved_at else "plan"
    for s in run.plan:
        if s["status"] == "failed":
            s["status"] = "pending"
    run.status, run.error, run.finished_at = ("running" if phase == "execute" else "planning"), "", None
    _append_log(run, "retried", by=user.username, phase=phase)
    _save(run, "plan", "status", "error", "finished_at", "log")
    if not launch(run, phase):
        raise RunDecisionError("This run is already being worked on.")


def _proposal_reviewable(user, proposal) -> bool:
    if proposal.run is not None:
        return can_review_run(user, proposal.run)
    metric = proposal.gap_metric
    membership = _membership(user, metric.workspace) if metric.workspace_id else None
    return bool(membership and membership.can_approve_agent_runs) or (not metric.workspace_id and metric.user_id == user.id)


def confirm_measurement(proposal, user) -> bool:
    """Record a proposed value as a real measurement (source='evidence').
    The single write path, record_gap_measurement, applies the same closure
    rule as a manual check-in. Returns True if it closed the gap."""
    from dashboard.views.helpers import record_gap_measurement

    if not _proposal_reviewable(user, proposal):
        raise RunDecisionError("You don't have permission to confirm measurements here.")
    if proposal.status != "pending":
        raise RunDecisionError("This measurement has already been reviewed.")
    note = f"{proposal.method} (computed by {proposal.agent_type or 'an agent'}, confirmed by {user.get_full_name() or user.username})"
    closed = record_gap_measurement(
        proposal.gap_metric, proposal.value, source="evidence", note=note[:240], recorded_by=user,
    )
    proposal.measurement = proposal.gap_metric.measurements.order_by("-measured_at").first()
    proposal.status, proposal.reviewed_by, proposal.reviewed_at = "accepted", user, timezone.now()
    proposal.save(update_fields=["measurement", "status", "reviewed_by", "reviewed_at"])
    if proposal.run is not None:
        _append_log(proposal.run, "measurement_confirmed", by=user.username, metric=proposal.gap_metric.metric)
        _save(proposal.run, "log")
    return closed


def reject_measurement(proposal, user) -> None:
    if not _proposal_reviewable(user, proposal):
        raise RunDecisionError("You don't have permission to review measurements here.")
    if proposal.status != "pending":
        raise RunDecisionError("This measurement has already been reviewed.")
    proposal.status, proposal.reviewed_by, proposal.reviewed_at = "rejected", user, timezone.now()
    proposal.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    if proposal.run is not None:
        _append_log(proposal.run, "measurement_rejected", by=user.username, metric=proposal.gap_metric.metric)
        _save(proposal.run, "log")


# ================================================================
# Starting a run from chat
# ================================================================

def build_run_starter_tool(agent_type: str, user, session=None, workspace=None):
    """The chat tool that hands real multi-step work to a run. Workspace
    agents without a session work against the workspace's latest completed
    assessment, so they get assessment data and document tools too."""
    from .models import AssessmentSession

    def start_multi_step_work(goal: str) -> str:
        """Start real, multi-step work on what the user asked for -- e.g.
        design and roll out a lead-scoring process, break a project into
        owned tasks, build a deliverable and assign follow-ups. You'll
        investigate and propose a plan; the user approves it in a review
        panel before anything changes, then you carry it out. Use this when
        the user wants something actually done that takes more than one
        action, not for questions you can answer in chat. `goal` restates
        exactly what they asked for, complete and specific.
        """
        run_session = session
        if run_session is None and workspace is not None:
            run_session = (
                AssessmentSession.objects.filter(workspace=workspace, is_completed=True).order_by("-created_at").first()
            )
        run = start_run(agent_type=agent_type, goal=goal, user=user, session=run_session, workspace=workspace)
        if run is None:
            return "Couldn't start that right now -- something is already running. Try again in a minute."
        return (
            "Started. I'm investigating and will propose a plan; nothing changes until it's approved. "
            f"Tell the user they'll get a notification, and give them this link to review the plan: {run_link(run)}"
        )

    return start_multi_step_work
