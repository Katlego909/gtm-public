# Deployment Plan: GTM Validator to Cloud Run

This plan outlines the steps to deploy the GTM Validator application to Google Cloud Run in the `us-west1` region, using Cloud SQL (PostgreSQL) as the database and Secret Manager for configuration.

## Objective
Deploy a robust, production-ready version of the GTM Validator to Cloud Run so it can be tested by others.

## Key Files & Context
- `docs/CLOUD_RUN_DEPLOYMENT.md`: Base deployment guide.
- `Dockerfile` & `docker-entrypoint.sh`: Container configuration.
- `gtm_validator/settings.py`: Django settings (already configured for `dj-database-url`).
- Target Project: `forge-497716`
- Target Region: `us-west1`

## Implementation Steps

### Phase 1: Infrastructure Setup [COMPLETE]
1.  **Project Configuration:** [DONE]
2.  **Cloud SQL Setup:** [DONE]
3.  **IAM Configuration:** [DONE]

### Phase 2: Secrets & Environment Variables [COMPLETE]
1.  **Create Secret Manager Secret:** [DONE]

### Phase 3: Build & Deploy [COMPLETE]
1.  **Build Container Image:** [DONE]
2.  **Deploy to Cloud Run:** [DONE]

### Phase 4: Verification [COMPLETE]
1.  **Check Logs:** [DONE]
2.  **Health Check:** [DONE]
3.  **Functional Testing:** [DONE]

## Verification Steps
- [x] Command `gcloud run services describe gtm-validator --region=us-west1` returns the service URL.
- [x] Opening the URL in a browser loads the landing page without errors.
- [x] AI Playbook generation works (verified via log confirmation of data loading).

