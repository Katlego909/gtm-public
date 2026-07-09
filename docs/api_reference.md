# Developer Reference: API & Views

This document provides a comprehensive technical reference for the GTM Validator's interaction layer. It details how the platform manages the assessment lifecycle, asynchronous AI processing, and multi-tenant security through a combination of standard Django views and HTMX-powered partials.

---

## 1. The Assessment Lifecycle (`gtm/views.py`)

The assessment process is managed through a single-view, multi-step pattern designed for high performance and zero data loss.

### View Pattern: `assessment_step`
This view dynamically renders questions based on the `current_step` of the `AssessmentSession`.

-   **Logic:** Detects the `HX-Request` header to return only the form partial for a seamless "Single Page App" feel.
-   **Security:** Wrapped in `safe_get_session_or_403` to prevent unauthorized access.
-   **State Persistence:** Every "Next" click triggers a `transaction.atomic` update that saves scores and context notes to `Response` objects.

```python
@login_required
def assessment_step(request, session_id, step: int):
    # 1. Authorization & Data Fetch
    session, is_authorized = safe_get_session_or_403(request, session_id)
    steps = _paginated_questions()
    questions = steps[step - 1]

    # 2. Dynamic Form Construction
    if request.method == "POST":
        form = StepForm(request.POST)
        if form.is_valid():
            # Atomic save ensures session state stays consistent
            with transaction.atomic():
                # Save Response objects...
                session.current_step = step + 1
                session.save()
            return redirect("gtm:assessment_step", session_id=session.uuid, step=step+1)
    
    # 3. Fragment Selection (HTMX)
    template = "gtm/assessment_step.html" if not _is_htmx(request) else "gtm/partials/step_form.html"
    return render(request, template, context)
```

---

## 2. Asynchronous AI Polling & State Management

Because AI generation (Playbooks, Diagnostics) is non-blocking, the frontend uses a polling pattern to update the UI once the background thread finishes.

### The "Polling Partial" Pattern
-   **`playbook_status`**: A view that checks the `ai_playbook_status` field.
-   **States:**
    -   `pending`: Triggers `_kickoff_playbook_generation`.
    -   `generating`: Returns an HTMX loading indicator with `hx-trigger="every 2s"`.
    -   `done`: Returns the final "View Playbook" button or content.
    -   `failed`: Returns a retry button.

### Implementation: AI Diagnostic Insights
Similar to the playbook, individual question insights are generated in batches to minimize latency.

```python
def insight_status(request, session_id, response_id):
    """
    Individual polling endpoint for question-level AI feedback.
    If 'pending', it spawns a background thread to call generate_diagnostic_insight.
    """
    response = get_object_or_404(Response.objects.select_related("question"), pk=response_id)
    if not response.ai_insight and response.ai_insight_status == "pending":
        # Spawn thread and mark status as 'generating'
        spawn_diagnostic_thread(response.id)
    
    return render(request, "gtm/partials/insight_status.html", {"response": response})
```

---

## 3. Real-Time Context Assist API

The `rewrite_context_note` view provides an "AI Assist" for users during the assessment.

-   **Endpoint:** `POST /gtm/assessment/<uuid>/rewrite-note/`
-   **Functionality:** Takes a raw user thought and returns a "cleaned" version optimized for the final AI strategy engine.
-   **Technical Detail:** Uses `rewrite_context_note_with_ai` (Gemini-Flash) with specific modes: `rewrite`, `summarize`, or `specific`.

---

## 4. Multimodal Audit Endpoints

The asset library (`dashboard/views.py`) uses specialized endpoints to trigger visual critiques.

-   **`trigger_resource_audit`**: Spawns an `audit_resource_async` thread for a specific `Resource`.
-   **Binary Handling:** The view reads the file from `default_storage` (local or Cloud Storage) and streams the bytes directly to the AI Auditor, avoiding public URL exposure.

---

## 5. The Chat Agent API (`dashboard/views.py`)

The dashboard features an agentic chat interface that can mutate the database state (e.g., creating tasks from chat).

### Agent Workflow
1.  **Request:** User sends a natural language prompt (e.g., "Create a task for the ICP gap").
2.  **Intent Detection:** `gtm/ai_chat.py` classifies the intent as `execution_plan`.
3.  **Command Execution:** The view calls `_run_dashboard_action_command` to create `ActionItem` or `GapAnalysisMetric` rows.
4.  **Response:** The agent returns a markdown confirmation which is rendered in the chat window.

---

## 6. Report Generation (`utils_pdf.py`)

The `download_report` view generates a professional executive PDF using **ReportLab**.

-   **Performance:** The PDF is generated in-memory using `BytesIO` and returned as a `FileResponse`.
-   **Security:** Generation requires the same `safe_get_session_or_403` check as the results page, ensuring sensitive strategic data is never exposed.
