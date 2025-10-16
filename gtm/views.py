from io import BytesIO
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count
from django.forms import Form, IntegerField
from django.forms.widgets import NumberInput
from django.db.models import Sum, F
from datetime import datetime
from .models import AssessmentSession, Question, Response, Category, RecommendationBand, ActionItem, ToolRecommendation
from django.utils.safestring import mark_safe
import math
from django.http import HttpResponse
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from django.utils.html import strip_tags
from django.views.decorators.http import require_POST
from django.shortcuts import redirect
from django.utils.safestring import mark_safe

LEGEND = {
    1: "No / Not in place",
    2: "Ad-hoc / Rarely",
    3: "In progress / Sometimes",
    4: "Consistent / Often",
    5: "Best-in-class / Always",
}

# ---------- helpers ----------
def _steps():
    return [list(cat.questions.all().order_by("id"))
            for cat in Category.objects.all().order_by("id")]
    
def _category_step_map():
    """Map category id → step number (1-based) for deep-linking to the wizard."""
    return {cat.id: idx + 1 for idx, cat in enumerate(Category.objects.all().order_by("id"))}    

def _first_incomplete_step(session):
    steps = _steps()
    for idx, qs in enumerate(steps, start=1):
        answered = Response.objects.filter(session=session, question__in=qs).count()
        if answered < len(qs):
            return idx
    return max(1, len(steps))  # all answered → last step

def _compute_scores(session):
    cat_scores = []
    cats = Category.objects.all()
    total_w = sum(c.weight for c in cats) or 1.0
    overall = 0.0
    for cat in cats:
        qs = cat.questions.all()
        rows = Response.objects.filter(session=session, question__in=qs)\
               .values_list("score", "question__weight")
        if rows:
            num = sum(s * w for s, w in rows)
            den = sum(w for _, w in rows) or 1.0
            avg = num / den
        else:
            avg = 0.0
        overall += (avg / 5.0) * (cat.weight / total_w) * 100.0
        cat_scores.append({"category": cat, "avg": avg})
    return cat_scores, overall

def _band_for_score(score):
    return RecommendationBand.objects.filter(min_score__lte=score, max_score__gte=score).first()

def _remember_session(request, sess_uuid):
    request.session.setdefault("gtm_sessions", [])
    if str(sess_uuid) not in request.session["gtm_sessions"]:
        request.session["gtm_sessions"].append(str(sess_uuid))
        request.session.modified = True       

def _paginated_questions():
    # keep your original ordering by category
    return [list(cat.questions.all().order_by("id"))
            for cat in Category.objects.all().order_by("id")]

def _is_session_complete(session: AssessmentSession) -> bool:
    total_q = Question.objects.count()
    if total_q == 0:
        return False
    answered_q = (Response.objects
                  .filter(session=session)
                  .values("question_id").distinct().count())
    return answered_q == total_q

def _first_incomplete_step(session: AssessmentSession) -> int:
    steps = _paginated_questions()
    for idx, qs in enumerate(steps, start=1):
        answered = (Response.objects
                    .filter(session=session, question__in=qs)
                    .values("question_id").distinct().count())
        if answered < len(qs):
            return idx
    return max(1, len(steps)) 
        
# ---------- Views ----------

def landing(request):
    return render(request, "gtm/landing.html")

def start_assessment(request):
    if request.method == "POST":
        company = request.POST.get("company_name", "")
        industry = request.POST.get("industry", "")
        session = AssessmentSession.objects.create(
            company_name=company, industry=industry,
            owner_client_id=_client_id(request)
        )
        return redirect("gtm:resume", session_id=session.uuid)
    return render(request, "gtm/start.html")

def resume_assessment(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    step = _first_incomplete_step(session)
    session.current_step = step
    session.save(update_fields=["current_step"])
    return redirect("gtm:assessment_step", session_id=session.uuid, step=step)

def resume_latest(request):
    cid = _client_id(request)
    s = AssessmentSession.objects.filter(owner_client_id=cid, is_completed=False).order_by("-created_at").first()
    if not s:
        return redirect("gtm:start")
    step = _first_incomplete_step(s)
    return redirect("gtm:assessment_step", session_id=s.uuid, step=step)


def _paginated_questions():
    """Return a list of steps, each = list[Question]. One category per step."""
    steps = []
    for cat in Category.objects.all().order_by("id"):
        steps.append(list(cat.questions.all().order_by("id")))
    return steps

def assessment_step(request, session_id, step: int):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    steps = _paginated_questions()
    total_steps = len(steps)
    if total_steps == 0:
        return redirect("gtm:results", session_id=session.uuid)

    # Do not allow skipping past the first incomplete step
    required_step = _first_incomplete_step(session)
    step = max(1, min(step, total_steps))
    if step > required_step:
        return redirect("gtm:assessment_step", session_id=session.uuid, step=required_step)

    questions = steps[step - 1]

    # Build a dynamic form with one integer field per question (1..5)
    class StepForm(Form):
        pass
    for q in questions:
        StepForm.base_fields[q.id_code] = IntegerField(
            min_value=1, max_value=5,
            widget=NumberInput(attrs={"class": "w-full border rounded px-3 py-2", "step": 1}),
            required=True,
            label=q.text
        )

    # Pre-fill if answers exist
    initial = {}
    existing = {r.question_id: r.score
                for r in Response.objects.filter(session=session, question__in=questions)}
    for q in questions:
        if q.id in existing:
            initial[q.id_code] = existing[q.id]

    if request.method == "POST":
        form = StepForm(request.POST, initial=initial)
        if form.is_valid():
            # save/update answers for this step
            for q in questions:
                score = form.cleaned_data[q.id_code]
                Response.objects.update_or_create(
                    session=session, question=q, defaults={"score": score}
                )

            # update progress + completion flag (data-driven)
            next_step = step + 1
            session.current_step = min(next_step, total_steps)
            session.is_completed = _is_session_complete(session)
            session.save(update_fields=["current_step", "is_completed"])

            if next_step > total_steps:
                return redirect("gtm:results", session_id=session.uuid)
            return redirect("gtm:assessment_step", session_id=session.uuid, step=next_step)
    else:
        form = StepForm(initial=initial)

    progress_pct = int((step - 1) / total_steps * 100)
    legend_html = "<br>".join([f"<b>{k}</b>: {v}" for k, v in LEGEND.items()])

    return render(request, "gtm/assessment_step.html", {
        "session": session, "form": form, "step": step, "total_steps": total_steps,
        "progress_pct": progress_pct, "legend": mark_safe(legend_html),
        "category": questions[0].category if questions else None
    })

def _compute_scores(session: AssessmentSession):
    # Per-category weighted average (1..5)
    cat_scores = []
    for cat in Category.objects.all():
        qs = cat.questions.all()
        if not qs.exists():
            continue
        rows = Response.objects.filter(session=session, question__in=qs).values("question__weight", "score")
        if not rows:
            avg = 0.0
        else:
            num = sum(r["question__weight"] * r["score"] for r in rows)
            den = sum(r["question__weight"] for r in rows)
            avg = num / den if den else 0.0
        cat_scores.append({"category": cat, "avg": avg})

    # Normalize category weights (e.g., 0.4/0.4/0.2)
    total_w = sum(c.weight for c in Category.objects.all()) or 1.0
    overall = 0.0
    for c in cat_scores:
        contrib = (c["avg"] / 5.0) * (c["category"].weight / total_w) * 100.0
        overall += contrib

    return cat_scores, overall

def _band_for_score(score: float):
    return RecommendationBand.objects.filter(min_score__lte=score, max_score__gte=score).first()

def results(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)

    # Step map (category id -> step index)
    step_map = {cat.id: idx + 1 for idx, cat in enumerate(Category.objects.all().order_by("id"))}

    # Categories
    strengths_categories = sorted(cat_scores, key=lambda x: x["avg"], reverse=True)[:3]
    focus_categories = sorted(cat_scores, key=lambda x: x["avg"])[:3]

    # Attach step to each category item
    for it in strengths_categories:
        it["step"] = step_map.get(it["category"].id)
    for it in focus_categories:
        it["step"] = step_map.get(it["category"].id)

    # Weakest questions (attach step)
    all_rows = []
    for q in Question.objects.all():
        r = Response.objects.filter(session=session, question=q).first()
        if r:
            all_rows.append({
                "question": q,
                "score": r.score,
                "weighted": r.score * q.weight,
                "note": q.diagnostic_note or "",
                "step": step_map.get(q.category_id),
            })
    weakest_questions = sorted(all_rows, key=lambda x: x["weighted"])[:3]

    # Radar chart data
    labels = [c["category"].name for c in cat_scores]
    values = [round(c["avg"], 2) for c in cat_scores]

    # -----------------------------
    # 🧩 Tool Recommendations Logic
    # -----------------------------
    from .models import ToolRecommendation

    recommendations = []
    for w in weakest_questions:
        q_text = (w["question"].text or "").lower()
        q_note = (w.get("note") or "").lower()
        cat = w["question"].category
        for m in ToolRecommendation.objects.filter(category=cat):
            kw = (m.keyword or "").lower()
            if kw and (kw in q_text or kw in q_note):
                recommendations.append(m)

    # Remove duplicates (by id) and keep a small, tidy list
    seen = set()
    uniq = []
    for r in recommendations:
        if r.id not in seen:
            seen.add(r.id)
            uniq.append(r)
    recommendations = uniq[:6]

    # -----------------------------
    # ✅ Render band actions as Markdown (nice bullets/headings)
    # -----------------------------
    band_actions_html = ""
    if band and getattr(band, "actions_markdown", ""):
        try:
            import markdown as md  # pip install markdown
            band_actions_html = md.markdown(
                band.actions_markdown,
                extensions=["extra", "sane_lists"]
            )
        except Exception:
            # graceful fallback if markdown not installed
            band_actions_html = band.actions_markdown.replace("\n", "<br>")
        band_actions_html = mark_safe(band_actions_html)

    return render(request, "gtm/results.html", {
        "session": session,
        "overall": round(overall, 1),
        "band": band,
        "band_actions_html": band_actions_html,  # <-- use this in template
        "cat_scores": cat_scores,
        "labels": labels,
        "values": values,
        "strengths_categories": strengths_categories,
        "focus_categories": focus_categories,
        "weakest_questions": weakest_questions,
        "recommendations": recommendations,
    })


def playbook(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)

    # sort categories by weakest first to highlight focus areas
    cat_sorted = sorted(cat_scores, key=lambda x: x["avg"])

    return render(request, "gtm/playbook.html", {
        "session": session,
        "overall": round(overall, 1),
        "band": band,
        "cat_scores": cat_scores,
        "cat_sorted": cat_sorted,
    })

def download_report_pdf(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)

    # Basic PDF (no external deps beyond reportlab)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 2*cm
    def line(text, size=12, dy=14):
        nonlocal y
        c.setFont("Helvetica-Bold" if size>=14 else "Helvetica", size)
        c.drawString(2*cm, y, text[:110])
        y -= dy

    line("Funti3r GTM Validator – Health Report", 16, 20)
    line(f"Company: {session.company_name or '-'}")
    line(f"Industry: {session.industry or '-'}")
    line(f"Date: {session.created_at.strftime('%Y-%m-%d %H:%M')}")
    y -= 8

    line(f"Overall Score: {round(overall,1)} / 100", 14, 18)
    if band:
        line(f"Stage: {band.stage}")
        line(f"Summary: {band.headline}")

    y -= 10
    line("Category Averages (1–5):", 13, 16)
    for cscore in cat_scores:
        line(f" - {cscore['category'].name}: {round(cscore['avg'],2)}")

    # Recommendations
    y -= 10
    line("Recommended Next Moves:", 13, 16)
    if band:
        # actions_markdown → plain text, split lines
        actions = strip_tags(band.actions_markdown).splitlines()
        for row in actions:
            if y < 2*cm:
                c.showPage(); y = height - 2*cm
            line(f" • {row}", 11, 13)

    c.showPage()
    c.save()
    pdf = buffer.getvalue()
    buffer.close()

    resp = HttpResponse(content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="gtm_report_{session.uuid}.pdf"'
    resp.write(pdf)
    return resp

def history(request):
    cid = _client_id(request)
    qs = AssessmentSession.objects.filter(owner_client_id=cid).order_by("-created_at")
    rows = []
    for s in qs:
        cat_scores, overall = _compute_scores(s) if s.is_completed else ([], None)
        band = _band_for_score(overall) if overall is not None else None
        rows.append({"session": s, "overall": (round(overall,1) if overall is not None else None), "band": band})
    return render(request, "gtm/history.html", {"rows": rows})


def _client_id(request):
    return request.COOKIES.get("gtm_client", "")

@require_POST
def action_add(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    # basic guard: same client owns this session
    if session.owner_client_id != _client_id(request):
        return redirect("gtm:results", session_id=session.uuid)

    note = request.POST.get("note","").strip()
    question_id = request.POST.get("question_id")
    if note:
        ActionItem.objects.create(
            session=session,
            question=Question.objects.filter(id=question_id).first() if question_id else None,
            note=note
        )
    return redirect("gtm:playbook", session_id=session.uuid)

@require_POST
def action_toggle(request, action_id):
    a = get_object_or_404(ActionItem, pk=action_id)
    # basic guard
    if a.session.owner_client_id != _client_id(request):
        return redirect("gtm:results", session_id=a.session.uuid)
    a.status = "done" if a.status != "done" else "todo"
    a.save(update_fields=["status"])
    return redirect("gtm:playbook", session_id=a.session.uuid)

@require_POST
def action_update(request, action_id):
    a = get_object_or_404(ActionItem, pk=action_id)
    # simple ownership guard
    if a.session.owner_client_id != _client_id(request):
        return redirect("gtm:playbook", session_id=a.session.uuid)

    note = request.POST.get("note", "").strip()
    owner = request.POST.get("owner", "").strip()
    status = request.POST.get("status", a.status)
    due_raw = request.POST.get("due_date", "").strip()

    a.note = note or a.note
    a.owner = owner
    if status in dict(ActionItem.STATUS_CHOICES):
        a.status = status
    # parse date safely (YYYY-MM-DD from <input type="date">)
    if due_raw:
        try:
            a.due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
        except ValueError:
            pass  # ignore bad date input
    else:
        a.due_date = None

    a.save()
    return redirect("gtm:playbook", session_id=a.session.uuid)

@require_POST
def action_delete(request, action_id):
    a = get_object_or_404(ActionItem, pk=action_id)
    if a.session.owner_client_id != _client_id(request):
        return redirect("gtm:playbook", session_id=a.session.uuid)
    sess_id = a.session.uuid
    a.delete()
    return redirect("gtm:playbook", session_id=sess_id)