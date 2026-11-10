"""Team assessment rounds. Rules and the merge live in gtm/team_rounds.py."""
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from gtm.decorators import workspace_member_required, workspace_permission_required
from gtm.models_team_rounds import TeamRound
from gtm.models_workspace import WorkspaceMembership
from gtm.services import _build_question_guidance
from gtm.team_rounds import (
    DISAGREEMENT_SPREAD, MIN_RESPONDENTS, RoundNotReady, applicable_questions, close_round,
    open_round, question_summaries, submit_answers,
)
from ..utils_notifications import send_notification

SCALE = [(1, "No / Not in place"), (2, "Ad-hoc / Rarely"), (3, "In progress / Sometimes"),
         (4, "Consistent / Often"), (5, "Best-in-class / Always")]


def _round_url(team_round):
    return reverse("team_round_detail", args=[team_round.workspace_id, team_round.pk])


@workspace_member_required()
@require_http_methods(["GET", "POST"])
def team_rounds(request, workspace_id):
    workspace = request.workspace
    members = (WorkspaceMembership.objects.filter(workspace=workspace, is_active=True)
               .select_related("user").order_by("user__username"))
    can_run = request.membership.can_run_team_rounds
    error = ""

    if request.method == "POST":
        if not can_run:
            return HttpResponseForbidden("Only admins and managers can open a round.")
        title = request.POST.get("title", "").strip()[:120] or "Team assessment"
        picked = set(request.POST.getlist("respondents"))
        users = [m.user for m in members if str(m.user_id) in picked]
        if len(users) < MIN_RESPONDENTS:
            error = f"Pick at least {MIN_RESPONDENTS} people to answer."
        else:
            team_round = open_round(workspace, request.user, title, users)
            answer_url = reverse("team_round_answer", args=[workspace.id, team_round.pk])
            for user in users:
                if user != request.user:
                    send_notification(
                        user, f"Your input is needed: {title}",
                        f"{request.user.get_username()} asked you to answer the {workspace.name} team assessment.",
                        sender=request.user, workspace=workspace, link=answer_url)
            messages.success(request, "Round opened. Respondents have been notified.")
            return redirect(_round_url(team_round))

    rounds = (TeamRound.objects.filter(workspace=workspace)
              .prefetch_related("respondents"))
    return render(request, "dashboard/team_rounds.html", {
        "workspace": workspace,
        "rounds": rounds,
        "members": members,
        "can_run": can_run,
        "min_respondents": MIN_RESPONDENTS,
        "error": error,
    })


@workspace_member_required()
def team_round_detail(request, workspace_id, round_id):
    team_round = get_object_or_404(TeamRound, pk=round_id, workspace=request.workspace)
    respondents = list(team_round.respondents.select_related("user").order_by("user__username"))
    mine = next((r for r in respondents if r.user_id == request.user.id), None)
    closed = team_round.status == "closed"
    summaries = question_summaries(team_round) if closed else []
    return render(request, "dashboard/team_round_detail.html", {
        "workspace": request.workspace,
        "round": team_round,
        "respondents": respondents,
        "submitted_count": sum(1 for r in respondents if r.submitted_at),
        "my_respondent": mine,
        "can_run": request.membership.can_run_team_rounds,
        "min_respondents": MIN_RESPONDENTS,
        "summaries": summaries,
        "flagged_count": sum(1 for s in summaries if s["flagged"]),
        "spread_threshold": DISAGREEMENT_SPREAD,
    })


@workspace_member_required()
@require_http_methods(["GET", "POST"])
def team_round_answer(request, workspace_id, round_id):
    team_round = get_object_or_404(TeamRound, pk=round_id, workspace=request.workspace)
    respondent = team_round.respondents.filter(user=request.user).first()
    if respondent is None or team_round.status != "open":
        messages.error(request, "This round is closed or you were not asked to answer it.")
        return redirect(_round_url(team_round))

    questions = applicable_questions(team_round)
    previous = {a.question_id: a for a in respondent.answers.all()}
    errors = set()

    if request.method == "POST":
        answers = {}
        for q in questions:
            raw = request.POST.get(f"q_{q.id}", "")
            if raw not in {"1", "2", "3", "4", "5"}:
                errors.add(q.id)
                continue
            answers[q.id] = (int(raw), request.POST.get(f"note_{q.id}", "").strip()[:2000])
        if not errors:
            submit_answers(respondent, answers)
            messages.success(request, "Your answers are in. Thank you.")
            return redirect(_round_url(team_round))

    rows = []
    for q in questions:
        prior = previous.get(q.id)
        rows.append({
            "question": q,
            "guidance": _build_question_guidance(q),
            "selected": request.POST.get(f"q_{q.id}") if request.method == "POST" else (str(prior.score) if prior else ""),
            "note": request.POST.get(f"note_{q.id}", "") if request.method == "POST" else (prior.note if prior else ""),
            "error": q.id in errors,
        })
    return render(request, "dashboard/team_round_answer.html", {
        "workspace": request.workspace,
        "round": team_round,
        "rows": rows,
        "scale": SCALE,
        "has_errors": bool(errors),
    })


@workspace_permission_required("can_run_team_rounds")
@require_POST
def team_round_close(request, workspace_id, round_id):
    team_round = get_object_or_404(TeamRound, pk=round_id, workspace=request.workspace)
    try:
        session = close_round(team_round, request.user)
    except RoundNotReady as exc:
        messages.error(request, str(exc))
        return redirect(_round_url(team_round))
    return redirect("gtm:results", session_id=session.uuid)
