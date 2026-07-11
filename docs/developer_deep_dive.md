# Developer Deep Dive: Master Architecture & Implementation

This document provides an exhaustive technical reference for the GTM Validator. It moves beyond high-level summaries to provide the **full, production-grade implementations** of the platform's core engines. This is the definitive "Source of Truth" for how the platform ensures resilience, security, and mathematical accuracy.

---

## 1. The UI Interaction Model: Django + HTMX

We utilize a **Modern Monolith** pattern where Django handles the state and HTMX handles the interactivity. This allows us to maintain a complex multi-step assessment wizard without the overhead of a JavaScript framework.

### The Implementation Pattern: Partial Rendering
Every assessment view is designed to detect HTMX requests and return only the necessary DOM fragments.

```python
def assessment_step(request, session_id, step: int):
    """
    Manages the multi-step assessment lifecycle.
    Detects HX-Request to return only the form partial for a seamless SPA experience.
    """
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return redirect("gtm:history")

    # Step-based question pagination logic
    steps = _paginated_questions()
    total_steps = len(steps)
    questions = steps[step - 1]

    # Optimized initial data fetch (prefills if answers exist)
    existing = {r.question_id: r for r in Response.objects.filter(session=session, question__in=questions)}
    initial = {q.id_code: existing[q.id].score for q in questions if q.id in existing}

    if request.method == "POST":
        form = StepForm(request.POST, initial=initial)
        if form.is_valid():
            with transaction.atomic():
                for q in questions:
                    Response.objects.update_or_create(
                        session=session, question=q,
                        defaults={"score": form.cleaned_data[q.id_code]}
                    )
                session.current_step = min(step + 1, total_steps)
                session.save(update_fields=["current_step"])
            return redirect("gtm:assessment_step", session_id=session.uuid, step=session.current_step)

    # Fragment Switching Logic
    template = "gtm/assessment_step.html" # Full Layout
    if request.headers.get('HX-Request') == 'true':
        template = "gtm/partials/step_form.html" # Partial Form

    return render(request, template, {
        "session": session, "form": form, "step": step, 
        "progress_pct": int((step - 1) / total_steps * 100),
        "is_htmx": True
    })
```

---

## 2. Resilient AI Engineering: The Playbook Engine

AI generation is brittle and slow. Our engine is built with a **three-tier resilience strategy**: Concurrency Guards, JSON Rescuing, and Static Fallbacks.

### Full Implementation: `generate_playbook_with_gemini`

```python
def generate_playbook_with_gemini(snapshot: ResultSnapshot) -> str:
    """
    Exhaustive AI Generation Pipeline:
    1. Lock Guard: Prevents duplicate concurrent generation.
    2. Status Management: Updates DB status for frontend polling.
    3. Content Rescue: Fixes malformed JSON or markdown-wrapped objects.
    4. Persistence: Saves cleaned markdown and structured analysis to the snapshot.
    """
    # 1. Concurrency Guard (Distributed Lock Pattern)
    playbook_lock_key = f"gtm:ai:playbook:{snapshot.id}:lock"
    if not cache.add(playbook_lock_key, "1", timeout=120):
        return (snapshot.ai_playbook or "").strip()

    snapshot.ai_playbook_status = "generating"
    snapshot.save(update_fields=["ai_playbook_status"])

    try:
        client = _get_client()
        if client and not _quota_cooldown_active():
            prompt = _build_prompt(snapshot)
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            raw_text = response.text.strip()
            
            # 2. Content Rescuing Pattern
            try:
                # Standard path: Clean markdown tags and parse JSON
                clean_json = _clean_json_response(raw_text)
                parsed = json.loads(clean_json)
                final_playbook = parsed.get("markdown_playbook", "")
                snapshot.ai_financial_summary = parsed.get("financial_summary", "")
                snapshot.ai_competitor_analysis = parsed.get("competitor_analysis", "")
            except Exception:
                # Rescue Path: Regex-based extraction if JSON is malformed
                match = re.search(r'["\']?markdown_playbook["\']?\s*:\s*["\']+(.*?)(?=["\'],\s*["\']|["\'],?\s*\}|$)', raw_text, re.DOTALL)
                final_playbook = match.group(1).replace('\\n', '\n').replace('\\"', '"') if match else raw_text

            # 3. Persistence & Status Finalization
            snapshot.ai_playbook = final_playbook
            snapshot.ai_playbook_status = "done"
            snapshot.save()
            return snapshot.ai_playbook

    except Exception as e:
        snapshot.ai_playbook_status = "failed"
        snapshot.save()
        logger.error(f"Playbook outer failure for {snapshot.id}: {e}")
        return ""
    finally:
        cache.delete(playbook_lock_key)
```

---

## 3. The Scoring Engine: Mathematical Accuracy

The GTM score is a weighted maturity index. We avoid "N+1" queries by performing the tiered mathematical aggregation directly in the database using SQL expressions.

### Full Implementation: `_compute_scores`

```python
def _compute_scores(session: AssessmentSession):
    """
    Computes hierarchical weighted maturity index.
    Tier 1: Question-level weights normalized within a category.
    Tier 2: Category-level weights (Demand: 0.4, Conversion: 0.4, Delivery: 0.2).
    """
    all_cats = Category.objects.all().order_by("id")
    total_w = sum(c.weight for c in all_cats) or 1.0
    cat_weight_map = {c.id: c.weight for c in all_cats}

    # Database Aggregation Engine
    # Sums (score * weight) and (weight) per category in ONE query.
    category_results = (
        Response.objects
        .filter(session=session)
        .values('question__category_id')
        .annotate(
            total_weighted_score=Sum(F('score') * F('question__weight')),
            total_weight=Sum('question__weight')
        )
        .order_by('question__category_id')
    )

    overall = 0.0
    scores_by_id = {c.id: {"category": c, "avg": 0.0, "pct": 0.0} for c in all_cats}

    for row in category_results:
        cat_id = row['question__category_id']
        num = row['total_weighted_score']
        den = row['total_weight']
        avg = num / den if den else 0.0
        
        # Normalize category average (1-5 scale) to percentage (0-100)
        scores_by_id[cat_id].update({"avg": avg, "pct": (avg / 5.0) * 100.0})
        
        # Apply Global Tier-2 Category Weighting
        weight = cat_weight_map.get(cat_id, 0)
        overall += (avg / 5.0) * (weight / total_w) * 100.0
        
    return list(scores_by_id.values()), round(overall, 1)
```

---

## 4. Security: Multi-Tenant Authorization

Our RBAC (Role-Based Access Control) ensures that data is only accessible to the owner or authorized team members. This logic is centralized in a single guard function.

### Full Implementation: `safe_get_session_or_403`

```python
def safe_get_session_or_403(request, session_id):
    """
    Centralized Security Guard for all Assessment data.
    Implements a three-tier hierarchy of access:
    1. Owner Check: Direct ownership via User FK.
    2. Workspace Check: Membership in the workspace that owns the session.
    3. Anonymous Check: Ownership verified via browser-scoped client_id cookie.
    """
    try:
        session = AssessmentSession.objects.get(pk=session_id)
    except (AssessmentSession.DoesNotExist, ValidationError):
        return None, False
    
    # Tier 3: Anonymous Access (Cookie-based Client ID)
    if not request.user.is_authenticated:
        if session.owner_client_id == _client_id(request):
            return session, True
        return None, False
    
    # Tier 1: Direct Ownership
    if session.user == request.user:
        return session, True
    
    # Tier 2: Collaborative Workspace Access
    if session.workspace:
        is_member = WorkspaceMembership.objects.filter(
            user=request.user, 
            workspace=session.workspace, 
            is_active=True
        ).exists()
        if is_member:
            return session, True
            
    return None, False
```

---

## 5. Vision-to-Scoring Bridge: The AI Auditor

The AI Auditor uses multimodal capabilities to turn visual strategic evidence (PDFs/Images) into numeric maturity modifiers.

### Full Implementation: `perform_resource_audit`

```python
def perform_resource_audit(resource) -> bool:
    """
    Orchestrates the lifecycle of a multimodal visual audit:
    1. Read: Pulls raw binary bytes from storage.
    2. Vision: Streams bytes + prompt to Gemini-Flash.
    3. Parse: Extracts 'Evidence Score' (1-10) using regex from AI prose.
    4. Map: Converts 1-10 score into a GTM Modifier (-3 to +3).
    """
    if resource.resource_type != 'file' or not resource.file:
        return False

    try:
        # Step 1: Binary Stream Capture
        resource.file.open('rb')
        file_bytes = resource.file.read()
        resource.file.close()

        # Step 2: Multimodal Critique
        audit_text = _run_multimodal_audit(
            file_bytes, 
            mime_type=_detect_mime(resource.file.name),
            asset_name=resource.get_category_display()
        )

        if audit_text:
            # Step 3: Qualitative-to-Quantitative Bridge
            match = re.search(r"evidence\s+score[:\s*]+(\d+)", audit_text, re.I)
            score = float(match.group(1)) if match else 5.0
            
            # Step 4: Numeric Mapping
            # 1-3 -> -3 (High Risk), 8-10 -> +3 (Strategic Asset)
            resource.ai_score_modifier = _map_score_to_modifier(score)
            resource.ai_audit_summary = audit_text
            resource.audit_status = 'complete'
            resource.save()
            return True
            
    except Exception as e:
        logger.error(f"Multimodal Audit failure for resource {resource.id}: {e}")
        resource.audit_status = 'failed'
        resource.save()
    
    return False
```
