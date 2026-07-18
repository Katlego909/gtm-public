# Maintenance & Operational Tools

This document provides a comprehensive guide to the command-line tools, deployment workflows, and operational procedures used to maintain the GTM Validator. It is intended for administrators and DevOps engineers responsible for the platform's stability and data integrity.

---

## 1. Django Management Commands

The platform includes several custom management commands located in `gtm/management/commands/`. These commands are **idempotent** and safe to run multiple times in any environment.

### Framework Initialization (`load_gtm_defaults`)
The most critical command for environment setup. It populates the core diagnostic framework.
-   **Usage:** `python manage.py load_gtm_defaults [--dry-run]`
-   **Substance:**
    -   **Categories:** Seeds the 3 core pillars (Demand, Conversion, Delivery).
    -   **Questions:** Loads 36+ weighted diagnostic questions with embedded `ai_metadata`.
    -   **Bands:** Defines the 5 maturity stages (e.g., "Foundation", "Scale") and their default strategic advice.
    -   **Logic:** Uses `update_or_create` to ensure wording updates are applied without duplicating records.

### Software Matchmaking (`load_tool_recommendations`)
Seeds the logic that suggests software tools based on assessment gaps.
-   **Usage:** `python manage.py load_tool_recommendations`
-   **Mechanism:** Maps specific GTM keywords (e.g., "CRM", "SEO", "Attribution") to tools. If a user scores low in a category where these keywords are present, the tool is suggested.

### Data Backfills (`backfill_owners`)
Maintenance tool for transitioning anonymous guest sessions to authenticated workspaces.
-   **Logic:** Scans `AssessmentSession` records for missing `user` or `workspace` links and attempts to associate them based on the `owner_client_id` historical cookie mapping.

---

## 2. Deployment Workflow: Google Cloud Platform

The GTM Validator is architected as a containerized "Modern Monolith" deployed on **Google Cloud Run**.

### Infrastructure Stack
-   **Compute:** Cloud Run (Serverless containers in `us-west1`).
-   **Database:** Cloud SQL (PostgreSQL).
-   **Secrets:** Secret Manager (Storing `DJANGO_SETTINGS_MODULE`, `DATABASE_URL`, and `GOOGLE_API_KEY`).
-   **CI/CD:** Cloud Build (Triggered via `cloudbuild.yaml`).

### Deployment Steps (Manual Override)
If manual deployment is required, the following workflow is used:
1.  **Build:** `gcloud builds submit --tag gcr.io/PROJECT_ID/gtm-validator`
2.  **Migrate:** Migrations are run as a Cloud Build step or a temporary Cloud Run Job to ensure schema consistency.
3.  **Deploy:**
    ```bash
    gcloud run deploy gtm-validator \
      --image gcr.io/PROJECT_ID/gtm-validator \
      --add-cloudsql-instances PROJECT_ID:REGION:INSTANCE \
      --update-secrets='DATABASE_URL=DATABASE_URL:latest'
    ```

---

## 3. Operational Monitoring

### AI Quota & Health Tracking
We monitor Vertex AI interaction through two primary channels:
-   **`AIUsageTracker`**: Captures token counts and feature-level usage in the database.
-   **Cloud Logging**: All AI failures (Quota hits, malformed JSON) are logged with the `[AI_ERROR]` prefix for easy filtering in the GCP Logs Explorer.

### Global Quota Cooldown
If the system detects a `ResourceExhausted` error (Rate limit hit), it activates a global cooldown:
-   **Cache Key:** `gtm:ai:quota_cooldown`
-   **Behavior:** All AI features (Playbooks, Chat) automatically switch to **Static Fallback Mode** for 5 minutes to prevent further API pressure.

---

## 4. Maintenance Procedures

### Rotating AI Models
To upgrade the underlying AI (e.g., from `gemini-2.5-flash` to a newer version):
1.  Update the `model_id` in `gtm/ai_services.py`.
2.  Run the test suite: `python manage.py test gtm.tests.test_ai_resilience` to ensure the "JSON Rescue" patterns still work with the new model's output format.

### Database Sanity Checks
Periodically run the following to ensure data integrity:
-   **Dangling Sessions:** `AssessmentSession.objects.filter(is_completed=False, created_at__lt=timezone.now()-timedelta(days=30)).delete()`
-   **Audit Health:** Verify that multimodal audits aren't stuck in `pending` status using the Django Admin dashboard.
