# Python Module Reference: Automated API Documentation

This page provides an automated, high-substance reference for the GTM Validator's core Python logic. We use `mkdocstrings` to pull the latest documentation directly from the source code.

---

## 📂 `gtm` Core Services

The `gtm` package contains the fundamental logic for the assessment engine and AI orchestration.

### 🧠 Assessment Services (`gtm.services`)
These services handle the mathematical core of the diagnostic, session safety, and UI guidance.

::: gtm.services
    options:
      members:
        - safe_get_session_or_403
        - _compute_scores
        - _build_question_guidance
        - _expand_gtm_jargon
        - _kickoff_playbook_generation

### 🤖 AI Strategy & Resilience (`gtm.ai_services`)
This module manages all interactions with Google Gemini (Vertex AI), including the "Rescue" patterns for malformed JSON and static fallbacks.

::: gtm.ai_services
    options:
      members:
        - generate_playbook_with_gemini
        - generate_diagnostic_insight
        - _clean_json_response
        - _normalize_ai_playbook_markdown
        - rewrite_context_note_with_ai
        - _is_quota_error

### 👁️ Multimodal AI Auditor (`gtm.ai_auditor`)
Handles the "Vision-to-Scoring" bridge by critiquing PDFs and images to generate numeric modifiers.

::: gtm.ai_auditor
    options:
      members:
        - perform_resource_audit
        - _extract_score_modifier
        - _run_multimodal_audit
        - audit_resource_async

---

## 📂 `dashboard` Analytics & Processing

The `dashboard` app manages the post-assessment experience, including asset parsing and workspace analytics.

### 📊 Workspace Analytics (`dashboard.analytics`)
Logic for calculating KPI trends and task velocity.

::: dashboard.analytics
    options:
      members:
        - get_dashboard_context
        - calculate_workspace_velocity

### 📄 Document Processing (`dashboard.document_processors`)
Hybrid OCR logic that combines local parsing with AI-powered vision fallbacks.

::: dashboard.document_processors
    options:
      members:
        - process_attachment
        - _extract_text_with_ai

---

## 🛠️ Utility & Monitoring

### 📈 AI Monitoring (`gtm.utils_ai_monitoring`)
Token-aware usage tracking and quota management.

::: gtm.utils_ai_monitoring
    options:
      members:
        - AIUsageTracker
        - log_usage
