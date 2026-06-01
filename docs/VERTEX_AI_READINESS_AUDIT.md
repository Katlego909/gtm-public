# Vertex AI & GCP Deployment Readiness Audit

**Status**: ⚠️ **Partially Ready** — Foundation exists but infrastructure gaps must be addressed before production GCP deployment.

---

## 📊 Current State Summary

### ✅ What's Already Done

| Component | Status | Details |
|-----------|--------|---------|
| **Vertex AI SDK** | ✅ Included | `google-cloud-aiplatform>=1.70.0` in requirements.txt |
| **GenAI Client** | ✅ Implemented | Using unified `google-genai` SDK with `vertexai=True` flag (ai_services.py:272) |
| **AI Services** | ✅ Integrated | Playbook generation uses Vertex AI (ai_services.py) |
| **Auth System** | ✅ Implemented | Django-allauth with user/workspace management |
| **Database Models** | ✅ Mature | Assessment sessions, workspaces, permissions scoped correctly |
| **CI/CD Pipeline** | ✅ Exists | `.github/workflows/ci.yml` with Python matrix testing |

### ❌ Critical Gaps for GCP Deployment

| Component | Status | Impact | Priority |
|-----------|--------|--------|----------|
| **GCP Settings** | ❌ Missing | `GCP_PROJECT_ID`, `GCP_LOCATION` not in settings.py | **P0** |
| **Service Account Auth** | ❌ Missing | No GOOGLE_APPLICATION_CREDENTIALS or ADC setup | **P0** |
| **Containerization** | ❌ Missing | No Dockerfile; required for Cloud Run | **P0** |
| **Cloud Run Config** | ❌ Missing | No service.yaml or deployment automation | **P0** |
| **Database** | ⚠️ SQLite | Won't scale; Cloud SQL not configured | **P1** |
| **Cloud Storage** | ❌ Missing | Media files stored locally; breaks in stateless Cloud Run | **P1** |
| **Deployment Docs** | ❌ Outdated | DEPLOYMENT.md is for PythonAnywhere, not GCP | **P1** |
| **Secrets Management** | ⚠️ Partial | Uses .env file; needs Secret Manager integration | **P1** |
| **Observability** | ❌ Missing | No Cloud Logging, Trace, or monitoring setup | **P2** |
| **Load Balancing** | ❌ Missing | No ingress/API Gateway config | **P2** |
| **Vertex AI Caching** | ❌ Missing | Could reduce costs with prompt caching | **P3** |

---

## 🔍 Detailed Findings

### 1. **Vertex AI Integration (Partially Complete)**

**Current Code** (gtm/ai_services.py:263-279):
```python
def _get_client():
    """Initializes and returns the Unified Google GenAI client for Vertex AI."""
    project_id = getattr(settings, "GCP_PROJECT_ID", None)
    location = getattr(settings, "GCP_LOCATION", "us-central1")
    
    if not GENAI_AVAILABLE or not project_id:
        return None
    try:
        return genai.Client(
            vertexai=True,
            project=project_id,
            location=location
        )
```

**Issues**:
- ❌ `GCP_PROJECT_ID` is never set in settings.py (line 14 checks for it but it defaults to None)
- ❌ `GCP_LOCATION` defaults to "us-central1" but user may need different region
- ⚠️ Falls back silently if project_id is None (no error messages to user)
- ⚠️ Uses `google.genai` but this is newer SDK; older code may reference `google.cloud.aiplatform` directly

**What Needs to Happen**:
```python
# settings.py should have:
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")  # Required for production
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
```

### 2. **Settings Configuration**

**Current gtm_validator/settings.py Issues**:
- ❌ No GCP_PROJECT_ID or GCP_LOCATION configuration
- ⚠️ Uses SQLite for DATABASE (not Cloud SQL)
- ⚠️ Uses local filesystem for MEDIA_ROOT (won't work in Cloud Run ephemeral storage)
- ❌ No DEFAULT_FILE_STORAGE configured (should use Cloud Storage)
- ❌ No LOGGING configuration for Cloud Logging

**Production Settings Needed**:
```python
# GCP Configuration
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")

# Database: Use Cloud SQL in production
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",  # or mysql
        "NAME": os.getenv("DB_NAME", "gtm_db"),
        "USER": os.getenv("DB_USER", "postgres"),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# Cloud Storage for Media Files
if not DEBUG:
    DEFAULT_FILE_STORAGE = "storages.backends.gcloud_storage.GoogleCloudStorage"
    GS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "")

# Secrets Manager Integration (optional but recommended)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-key")
```

### 3. **Deployment Infrastructure**

**Missing Files**:

1. ❌ **Dockerfile** — Required for Cloud Run
2. ❌ **.dockerignore** — Optimization
3. ❌ **cloudbuild.yaml** — CI/CD pipeline for Cloud Build
4. ❌ **cloud-run-service.yaml** — Service configuration
5. ❌ **app.yaml** — If using App Engine
6. ❌ **Procfile** — If using any PaaS

**Example Dockerfile Needed**:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN python manage.py collectstatic --noinput
CMD ["gunicorn", "-b", "0.0.0.0:8080", "gtm_validator.wsgi:application"]
```

### 4. **Database & Persistence**

**Current**: SQLite (db.sqlite3)
**Problem**: Cloud Run instances are stateless; local files disappear after restart

**Required Changes**:
- ✅ Migrate to Cloud SQL (PostgreSQL or MySQL)
- ✅ Use Cloud Storage for media files (MEDIA_ROOT should be GCS bucket)
- ✅ Update django-storages: `pip install django-storages`
- ✅ Add google-cloud-storage: `pip install google-cloud-storage`

### 5. **Authentication & Secrets**

**Current**: Uses .env file with raw API keys (GEMINI_API_KEY, EMAIL credentials)

**Problems**:
- ❌ .env files don't work in containerized environments
- ❌ Secrets are not encrypted at rest
- ❌ Hard to rotate keys without redeploying

**Required**:
- Migrate to Google Cloud Secret Manager
- Update settings.py to fetch from Secret Manager at runtime
- Use Application Default Credentials (ADC) for GCP service access

### 6. **CI/CD Pipeline**

**Current** (.github/workflows/ci.yml):
- ✅ Runs Django tests
- ✅ Tests migrations
- ❌ No deployment step to GCP
- ❌ No Docker image build
- ❌ No Cloud Run deployment

---

## 🛠️ Implementation Roadmap

### Phase 1: Configuration & Secrets (P0 - Do First)
- [ ] Add GCP_PROJECT_ID and GCP_LOCATION to settings.py
- [ ] Set up Google Cloud service account with Vertex AI permissions
- [ ] Set up Secret Manager for all sensitive values
- [ ] Update environment variable documentation
- [ ] Test Vertex AI client initialization with real credentials

### Phase 2: Containerization & Storage (P0 - Blocking Deployment)
- [ ] Create Dockerfile with proper Python + Django setup
- [ ] Add .dockerignore
- [ ] Configure Cloud SQL connection in settings
- [ ] Integrate Cloud Storage for media files
- [ ] Add google-cloud-storage to requirements.txt

### Phase 3: Deployment Automation (P0 - Enables actual deployment)
- [ ] Write cloudbuild.yaml for automated CI/CD
- [ ] Create Cloud Run service configuration
- [ ] Set up database migrations in deployment pipeline
- [ ] Test full deployment flow locally with Cloud Run emulator

### Phase 4: Observability & Monitoring (P1 - Production Readiness)
- [ ] Configure Cloud Logging
- [ ] Set up Cloud Trace
- [ ] Add monitoring dashboards
- [ ] Set up error alerting

### Phase 5: Optimization (P2 - Performance & Cost)
- [ ] Implement Vertex AI prompt caching
- [ ] Add Redis for caching (Cloud Memorystore)
- [ ] Optimize database queries
- [ ] Cost analysis and budget alerts

---

## ✅ Pre-Deployment Checklist

Before deploying to GCP:

- [ ] GCP project created and linked to billing
- [ ] Service account created with these roles:
  - `aiplatform.user` — Vertex AI access
  - `secretmanager.secretAccessor` — Secret Manager
  - `cloudsql.client` — Cloud SQL access (if using)
  - `storage.objectAdmin` — Cloud Storage access
- [ ] Cloud SQL instance created (PostgreSQL 13+)
- [ ] Cloud Storage bucket created for media
- [ ] All secrets migrated to Secret Manager
- [ ] Docker image builds locally: `docker build -t gtm .`
- [ ] Cloud Run deployment tested: `gcloud run deploy gtm`
- [ ] Database migrations run successfully
- [ ] Admin user created in production database
- [ ] Static files collected and served
- [ ] Email configuration tested (SendGrid/Gmail App Password)
- [ ] Vertex AI quota sufficient for expected load
- [ ] SSL/HTTPS enforced (Cloud Run default)
- [ ] Monitoring dashboards set up

---

## 📋 Environment Variables Reference (GCP)

| Variable | Required | Location | Example |
|----------|----------|----------|---------|
| `GCP_PROJECT_ID` | Yes | Secret Manager | `my-gtm-project` |
| `GCP_LOCATION` | No | Secret Manager | `us-central1` |
| `DATABASE_URL` | Yes | Secret Manager | `postgresql://user:pass@host:5432/db` |
| `DJANGO_SECRET_KEY` | Yes | Secret Manager | `django-insecure-...` (generate new) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes | Cloud Run IAM | Auto-injected by Cloud Run |
| `EMAIL_HOST_USER` | Yes | Secret Manager | Email address |
| `EMAIL_HOST_PASSWORD` | Yes | Secret Manager | App password or SendGrid API key |
| `GCS_BUCKET_NAME` | Yes | Secret Manager | `gtm-media-us-central1` |
| `ALLOWED_HOSTS` | Yes | Secret Manager | `gtm.example.com,*.run.app` |
| `DEBUG` | No | Secret Manager | `False` (always) |

---

## 🎯 Next Steps

1. **Start with Phase 1** — Get GCP credentials and settings configured (1-2 hours)
2. **Test Vertex AI** — Verify connection works with real project (30 mins)
3. **Move to Phase 2** — Containerize and migrate database (3-4 hours)
4. **Test locally** — Build Docker image, run with Cloud Run emulator (2 hours)
5. **Deploy to Cloud Run** — First production deployment (1-2 hours)
6. **Add observability** — Set up logging and monitoring (2-3 hours)

**Estimated Total Time**: 10-15 hours of focused work across multiple sessions

---

## 📚 References

- [Vertex AI Python Client Docs](https://cloud.google.com/python/docs/reference/aiplatform/latest)
- [Cloud Run Django Deployment](https://cloud.google.com/run/docs/quickstarts/build-and-deploy/python)
- [Cloud SQL Django Connection](https://cloud.google.com/sql/docs/postgres/django-appengine)
- [Cloud Storage Django Integration](https://django-storages.readthedocs.io/en/latest/backends/gcloud.html)
- [Secret Manager for Django](https://cloud.google.com/python/docs/reference/secretmanager/latest)

---

**Generated**: 2026-05-28 | **Audit Scope**: Full GCP deployment readiness
