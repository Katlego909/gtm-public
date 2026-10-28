"""Review panel for agent runs: see the plan an agent proposed, approve or
send it back, answer the agent's questions, and follow its progress. All
decisions go through gtm/agent_runs.py, which owns the run lifecycle and
its permission checks."""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from gtm.agent_runs import (
    RunDecisionError,
    STEP_KINDS,
    answer_question,
    approve_run,
    can_answer_question,
    can_review_run,
    can_view_run,
    cancel_run,
    confirm_measurement,
    reject_measurement,
    reject_run,
    retry_run,
)
from dashboard.models import GapMeasurementProposal
from gtm.models import ActionItem, AgentDocument, AgentRun, AgentRunQuestion

STEP_KIND_LABELS = {
    "create_document": "Document",
    "create_subtasks": "Tasks",
    "assign_task": "Assignment",
    "ask_teammate": "Question",
    "notify_teammate": "Notification",
    "comment": "Comment",
    "recommend_resource": "Resource",
    "flag_gap": "Gap",
    "propose_measurement": "Measurement",
    "crm_update": "CRM",
}


def _get_run_for_user(request, run_id) -> AgentRun:
    run = get_object_or_404(
        AgentRun.objects.select_related("workspace", "session", "action_item", "requested_by", "approved_by"),
        pk=run_id,
    )
    if not can_view_run(request.user, run):
        raise Http404
    return run


def _step_rows(run: AgentRun):
    """Plan steps with readable labels and links to what each one produced."""
    refs = [r for s in run.plan for r in s.get("refs", [])]
    doc_ids = {r["document_id"] for r in refs if r.get("document_id")}
    task_ids = {r["task_id"] for r in refs if r.get("task_id")}
    docs = {str(d.pk): d for d in AgentDocument.objects.filter(pk__in=doc_ids)}
    tasks = {t.pk: t for t in ActionItem.objects.filter(pk__in=task_ids)}

    rows = []
    for step in run.plan:
        produced = []
        for ref in step.get("refs", []):
            if ref.get("document_id") in docs:
                produced.append({"kind": "document", "obj": docs[ref["document_id"]]})
            elif ref.get("task_id") in tasks:
                produced.append({"kind": "task", "obj": tasks[ref["task_id"]]})
        rows.append({
            **step,
            "kind_label": STEP_KIND_LABELS.get(step["kind"], step["kind"]),
            "kind_help": STEP_KINDS.get(step["kind"], ("",))[0],
            "produced": produced,
        })
    return rows


def _render_panel(request, run: AgentRun, error: str = "", changed: bool = False):
    from gtm.agent_runs import run_lock_key
    from gtm.ai_services import _lock_is_held

    run.refresh_from_db()
    # A planning/running run whose worker died (no lock held) is stalled:
    # stop polling and offer a retry instead of spinning forever.
    is_active = run.status in AgentRun.ACTIVE_STATUSES
    is_stalled = is_active and not _lock_is_held(run_lock_key(run))
    questions = list(run.questions.select_related("asked_to", "answered_by"))
    context = {
        "run": run,
        "steps": _step_rows(run),
        "questions": [
            {"q": q, "can_answer": q.is_open and can_answer_question(request.user, q)} for q in questions
        ],
        "proposals": list(run.measurement_proposals.select_related("gap_metric", "reviewed_by")),
        "can_review": can_review_run(request.user, run),
        "is_active": is_active and not is_stalled,
        "is_stalled": is_stalled,
        "error": error,
    }
    response = render(request, "dashboard/partials/agent_run_panel.html", context)
    if changed:
        response["HX-Trigger"] = "agentRunChanged"
    return response


@login_required
def agent_run_panel(request, run_id):
    return _render_panel(request, _get_run_for_user(request, run_id))


def _decide(request, run_id, decision):
    run = _get_run_for_user(request, run_id)
    try:
        decision(run)
    except RunDecisionError as e:
        return _render_panel(request, run, error=str(e))
    return _render_panel(request, run, changed=True)


@login_required
@require_POST
def agent_run_approve(request, run_id):
    return _decide(request, run_id, lambda run: approve_run(run, request.user, request.POST.getlist("keep_step")))


@login_required
@require_POST
def agent_run_reject(request, run_id):
    revise = request.POST.get("revise") == "1"
    return _decide(
        request, run_id, lambda run: reject_run(run, request.user, request.POST.get("feedback", ""), revise=revise),
    )


@login_required
@require_POST
def agent_run_cancel(request, run_id):
    return _decide(request, run_id, lambda run: cancel_run(run, request.user))


@login_required
@require_POST
def agent_run_retry(request, run_id):
    return _decide(request, run_id, lambda run: retry_run(run, request.user))


@login_required
@require_POST
def agent_run_answer(request, run_id, question_id):
    run = _get_run_for_user(request, run_id)
    question = get_object_or_404(AgentRunQuestion, pk=question_id, run=run)
    try:
        answer_question(question, request.user, request.POST.get("answer", ""))
    except RunDecisionError as e:
        return _render_panel(request, run, error=str(e))
    return _render_panel(request, run, changed=True)


def _decide_measurement(request, run_id, proposal_id, decision):
    run = _get_run_for_user(request, run_id)
    proposal = get_object_or_404(GapMeasurementProposal.objects.select_related("gap_metric"), pk=proposal_id, run=run)
    try:
        decision(proposal)
    except RunDecisionError as e:
        return _render_panel(request, run, error=str(e))
    return _render_panel(request, run, changed=True)


@login_required
@require_POST
def agent_run_confirm_measurement(request, run_id, proposal_id):
    return _decide_measurement(request, run_id, proposal_id, lambda p: confirm_measurement(p, request.user))


@login_required
@require_POST
def agent_run_reject_measurement(request, run_id, proposal_id):
    return _decide_measurement(request, run_id, proposal_id, lambda p: reject_measurement(p, request.user))
