# gtm/score_comparison.py
"""
Computes a "since your last assessment" delta between the current session's
freshly-saved ResultSnapshot and the most recent qualifying prior completed
assessment. Nothing here is persisted -- computed fresh at results-page
render time, same as the rest of gtm/views/results.py, so there's no
staleness class of bug to worry about.

"Prior qualifying assessment" policy:
- Workspace-scoped session: the most recent OTHER completed session in the
  same workspace (mirrors gtm/workspace_agent_services.py's
  get_workspace_snapshots query -- workspace is the trust boundary for
  "same company" there too).
- No-workspace (solo/anonymous-turned-user) session: the most recent OTHER
  completed session by the same user, but ONLY if company_name matches
  case-insensitively -- prevents silently diffing two unrelated companies
  assessed under one account.
"""
from .models import AssessmentSession, Response
from .scoring_engine import build_recommendation_context


def _find_previous_session(session: AssessmentSession):
    if session.workspace_id:
        qs = AssessmentSession.objects.filter(workspace_id=session.workspace_id, is_completed=True)
    else:
        if not session.user_id:
            return None
        company = (session.company_name or "").strip()
        if not company:
            return None
        qs = AssessmentSession.objects.filter(
            user_id=session.user_id, workspace__isnull=True, is_completed=True,
            company_name__iexact=company,
        )
    return (
        qs.exclude(pk=session.pk)
        .filter(created_at__lt=session.created_at)
        .select_related("snapshot")
        .order_by("-created_at")
        .first()
    )


def build_score_comparison(session: AssessmentSession, current_context: dict | None = None) -> dict | None:
    """Returns None when there's no qualifying prior assessment to compare
    against (including: this is the first assessment ever, or the current
    session itself has no snapshot yet)."""
    current_snapshot = getattr(session, "snapshot", None)
    if current_snapshot is None:
        return None

    previous_session = _find_previous_session(session)
    if previous_session is None or not hasattr(previous_session, "snapshot"):
        return None
    previous_snapshot = previous_session.snapshot

    # Category deltas -- category_breakdown entries use the stable Category
    # name string as key (see gtm/services.py::_save_snapshot), safe to join
    # directly without an ID lookup.
    current_by_cat = {row["category"]: row["avg"] for row in current_snapshot.category_breakdown}
    previous_by_cat = {row["category"]: row["avg"] for row in previous_snapshot.category_breakdown}
    all_categories = list(dict.fromkeys(list(previous_by_cat) + list(current_by_cat)))
    category_deltas = []
    for cat in all_categories:
        prev = previous_by_cat.get(cat)
        curr = current_by_cat.get(cat)
        category_deltas.append({
            "category": cat,
            "previous": prev,
            "current": curr,
            "delta": round(curr - prev, 2) if (prev is not None and curr is not None) else None,
        })

    # Pattern diff -- recompute the previous session's patterns fresh from
    # its own permanent Response rows (never overwritten across sessions);
    # reuse the current session's context when the caller already built one.
    if current_context is None:
        current_q_scores = {
            r.question.id_code: r.score
            for r in Response.objects.filter(session=session).select_related("question")
        }
        current_context = build_recommendation_context(current_q_scores) if current_q_scores else {}

    previous_q_scores = {
        r.question.id_code: r.score
        for r in Response.objects.filter(session=previous_session).select_related("question")
    }
    previous_context = build_recommendation_context(previous_q_scores) if previous_q_scores else {}

    current_patterns = {p["name"]: p for p in current_context.get("patterns", [])}
    previous_patterns = {p["name"]: p for p in previous_context.get("patterns", [])}
    newly_triggered_patterns = [current_patterns[name] for name in current_patterns if name not in previous_patterns]
    resolved_patterns = [previous_patterns[name] for name in previous_patterns if name not in current_patterns]

    # Radar overlay -- aligned to the CURRENT radar_labels order so the
    # chart's existing labels array can be reused unchanged; a pillar only
    # scored this time (e.g. a company_stage change) becomes 0 so Chart.js
    # still renders a closed polygon rather than erroring.
    previous_radar_values = [previous_by_cat.get(label, 0) for label in current_snapshot.radar_labels]

    return {
        "previous_session": previous_session,
        "previous_created_at": previous_session.created_at,
        "overall_delta": round(current_snapshot.overall - previous_snapshot.overall, 1),
        "previous_overall": previous_snapshot.overall,
        "band_changed": current_snapshot.band_stage != previous_snapshot.band_stage,
        "previous_band_stage": previous_snapshot.band_stage,
        "previous_band_headline": previous_snapshot.band_headline,
        "category_deltas": category_deltas,
        "newly_triggered_patterns": newly_triggered_patterns,
        "resolved_patterns": resolved_patterns,
        "previous_radar_values": previous_radar_values,
    }
