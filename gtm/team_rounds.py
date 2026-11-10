"""
Team round rules (decided 2026-11-10).

- An admin or manager opens a round and picks respondents. Each answers the
  applicable questions privately; nobody sees anyone else's answers while the
  round is open.
- Closing needs at least MIN_RESPONDENTS submissions. It creates one
  consensus AssessmentSession in the workspace whose score per question is the
  respondents' average, rounded half up (Response.score is a whole 1-5 number
  everywhere in scoring). The round page keeps the exact average and spread.
- A question is flagged when answers differ by DISAGREEMENT_SPREAD or more
  points. The flag and respondents' notes go into the consensus response's
  context_note, so the AI insight for that question sees the disagreement.
- After closing, every workspace member can see who answered what. The
  answer page tells respondents this up front.
"""
import math

from django.db import transaction
from django.utils import timezone

from .models import AssessmentSession, Question, Response
from .models_team_rounds import TeamRound, TeamRoundAnswer, TeamRoundRespondent
from .services import _applicable_categories

MIN_RESPONDENTS = 2
DISAGREEMENT_SPREAD = 2
FIRMOGRAPHIC_FIELDS = ("company_name", "industry", "website", "company_size", "revenue_range", "country", "crm")


class RoundNotReady(Exception):
    pass


def latest_workspace_assessment(workspace):
    return (AssessmentSession.objects.filter(workspace=workspace, is_completed=True)
            .order_by("-created_at").first())


def applicable_questions(team_round):
    return list(Question.objects.filter(category__in=_applicable_categories(team_round))
                .select_related("category").order_by("category_id", "id"))


def open_round(workspace, created_by, title, respondent_users):
    latest = latest_workspace_assessment(workspace)
    with transaction.atomic():
        team_round = TeamRound.objects.create(
            workspace=workspace, created_by=created_by, title=title,
            company_stage=latest.company_stage if latest else "",
        )
        TeamRoundRespondent.objects.bulk_create(
            [TeamRoundRespondent(round=team_round, user=u) for u in respondent_users])
    return team_round


def submit_answers(respondent, answers):
    """answers: {question_id: (score, note)}. Replaces earlier answers."""
    with transaction.atomic():
        respondent.answers.all().delete()
        TeamRoundAnswer.objects.bulk_create([
            TeamRoundAnswer(respondent=respondent, question_id=qid, score=score, note=note)
            for qid, (score, note) in answers.items()
        ])
        respondent.submitted_at = timezone.now()
        respondent.save(update_fields=["submitted_at"])


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def question_summaries(team_round):
    """Per applicable question: each respondent's answer, exact average, spread, flag."""
    questions = applicable_questions(team_round)
    answers = (TeamRoundAnswer.objects
               .filter(respondent__round=team_round, respondent__submitted_at__isnull=False)
               .select_related("respondent__user"))
    by_question = {}
    for a in answers:
        by_question.setdefault(a.question_id, []).append(a)

    summaries = []
    for q in questions:
        rows = sorted(by_question.get(q.id, []), key=lambda a: a.respondent.user.username)
        scores = [a.score for a in rows]
        average = sum(scores) / len(scores) if scores else None
        spread = max(scores) - min(scores) if scores else 0
        summaries.append({
            "question": q,
            "answers": rows,
            "average": average,
            "consensus": _round_half_up(average) if average is not None else None,
            "spread": spread,
            "flagged": spread >= DISAGREEMENT_SPREAD,
        })
    return summaries


def _context_note(summary):
    parts = []
    if summary["flagged"]:
        scores = sorted(a.score for a in summary["answers"])
        parts.append(
            f"Team disagreement: answers ranged from {scores[0]} to {scores[-1]} "
            f"across {len(scores)} respondents (exact average {summary['average']:.1f}).")
    notes = [a.note.strip() for a in summary["answers"] if a.note.strip()]
    if notes:
        parts.append("Team notes: " + " | ".join(notes))
    return "\n".join(parts)


def close_round(team_round, closed_by):
    with transaction.atomic():
        team_round = TeamRound.objects.select_for_update().get(pk=team_round.pk)
        if team_round.status != "open":
            raise RoundNotReady("This round is already closed.")
        submitted = team_round.respondents.filter(submitted_at__isnull=False).count()
        if submitted < MIN_RESPONDENTS:
            raise RoundNotReady(f"At least {MIN_RESPONDENTS} people need to submit before closing.")

        latest = latest_workspace_assessment(team_round.workspace)
        workspace = team_round.workspace
        firmographics = ({f: getattr(latest, f) for f in FIRMOGRAPHIC_FIELDS} if latest else {
            "company_name": workspace.name, "industry": workspace.industry,
            "company_size": workspace.company_size, "website": workspace.website,
        })
        summaries = question_summaries(team_round)
        if any(s["consensus"] is None for s in summaries):
            raise RoundNotReady("Some questions have no answers yet.")
        session = AssessmentSession.objects.create(
            owner_client_id=f"team-round-{team_round.pk}",
            user=closed_by,
            workspace=workspace,
            company_stage=team_round.company_stage,
            notes=f"Consensus of team round \"{team_round.title}\" ({submitted} respondents).",
            is_completed=True,
            current_step=len({s["question"].category_id for s in summaries}),
            **firmographics,
        )
        Response.objects.bulk_create([
            Response(session=session, question=s["question"], score=s["consensus"], context_note=_context_note(s))
            for s in summaries
        ])
        team_round.status = "closed"
        team_round.closed_at = timezone.now()
        team_round.consensus_session = session
        team_round.save(update_fields=["status", "closed_at", "consensus_session"])
    return session
