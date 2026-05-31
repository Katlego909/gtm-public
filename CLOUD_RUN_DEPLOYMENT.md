# Cloud Run Deployment Guide

This guide covers deploying the GTM Validator Django application to Google Cloud Run.

## Prerequisites

1. A Google Cloud Project with billing enabled
2. `gcloud` CLI installed and authenticated
3. Docker (for local testing)
4. A Cloud SQL PostgreSQL instance (for database)
5. A service account with appropriate permissions

## Setup Steps

### 1. Create a Cloud SQL PostgreSQL Instance

```bash
# Create PostgreSQL instance
gcloud sql instances create gtm-db \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --availability-type=zonal
```

### 2. Create a Database and User

```bash
# Create the database
gcloud sql databases create gtm_validator --instance=gtm-db

# Create a database user
gcloud sql users create gtm_user --instance=gtm-db --password

# Save the password securely (you'll need it for DATABASE_URL)
```

### 3. Set Up a Service Account

```bash
# Create a service account
gcloud iam service-accounts create gtm-cloud-run \
  --display-name="GTM Cloud Run Service Account"

# Grant necessary permissions
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:gtm-cloud-run@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/cloudsql.client

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:gtm-cloud-run@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/storage.admin

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:gtm-cloud-run@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/aiplatform.user

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:gtm-cloud-run@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/storage.objects.get
```

### 4. Build and Push Docker Image

```bash
# Set your project ID
export PROJECT_ID=$(gcloud config get-value project)

# Build the image
docker build -t gcr.io/${PROJECT_ID}/gtm-validator:latest .

# Push to Container Registry
docker push gcr.io/${PROJECT_ID}/gtm-validator:latest
```

### 5. Create a Secret for Django Settings

Create a `.env.cloud` file with production settings:

```bash
# Create a Secret Manager secret
gcloud secrets create django-env --data-file=.env.cloud

# Or create interactively:
echo "DJANGO_SECRET_KEY=your-long-random-secret-key" | \
  gcloud secrets create django-env --data-file=-
```

### 6. Grant Cloud Run Service Account Access to Secret

```bash
gcloud secrets add-iam-policy-binding django-env \
  --member=serviceAccount:gtm-cloud-run@${PROJECT_ID}.iam.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor
```

## Required Environment Variables

Set these when deploying to Cloud Run:

```bash
# Django Configuration
DJANGO_SECRET_KEY=your-long-random-secret-key
DEBUG=False
ALLOWED_HOSTS=your-domain.com,*.cloudrun.app

# Database Configuration
DATABASE_URL=postgresql://gtm_user:PASSWORD@/gtm_validator?host=/cloudsql/PROJECT_ID:us-central1:gtm-db

# Google Cloud Configuration
GCP_PROJECT_ID=your-project-id

# Email Configuration
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
EMAIL_REDIRECT_TO=your-email@gmail.com  # For testing

# Security
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
```

## Deploy to Cloud Run

### Option 1: Using gcloud CLI (Recommended)

```bash
gcloud run deploy gtm-validator \
  --image=gcr.io/${PROJECT_ID}/gtm-validator:latest \
  --platform=managed \
  --region=us-central1 \
  --allow-unauthenticated \
  --service-account=gtm-cloud-run@${PROJECT_ID}.iam.gserviceaccount.com \
  --add-cloudsql-instances=PROJECT_ID:us-central1:gtm-db \
  --set-env-vars=\
DJANGO_SECRET_KEY=your-secret-key,\
DEBUG=False,\
ALLOWED_HOSTS=*.cloudrun.app,\
GCP_PROJECT_ID=${PROJECT_ID} \
  --memory=512Mi \
  --cpu=1 \
  --timeout=120 \
  --max-instances=10 \
  --min-instances=1
```

### Option 2: Using Cloud Build (CI/CD)

```bash
# Set up automated builds from your repository
gcloud builds submit \
  --config=cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_SERVICE_NAME=gtm-validator
```

## Post-Deployment Steps

### 1. Update Django Settings

Update `gtm_validator/settings.py` to use environment-based configuration:

```python
# In settings.py, add:
import dj_database_url

DATABASES = {
    "default": dj_database_url.config(
        default=os.getenv(
            "DATABASE_URL",
            "sqlite:///db.sqlite3"
        ),
        conn_max_age=600,
    )
}
```

Add `dj-database-url` to `requirements.txt`:

```bash
echo "dj-database-url>=2.0.0" >> requirements.txt
```

### 2. Update Security Settings

Add to `gtm_validator/settings.py`:

```python
# Security settings for production
if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True") == "True"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "True") == "True"
    CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "True") == "True"
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "True") == "True"
```

### 3. Test the Deployment

```bash
# Check service status
gcloud run services describe gtm-validator --region=us-central1

# View logs
gcloud run services logs read gtm-validator --region=us-central1 --limit=50

# Test the endpoint
curl https://gtm-validator-xxxxx.a.run.app/health/
```

## Scaling Configuration

Adjust these parameters based on traffic:

```bash
# Update auto-scaling
gcloud run services update gtm-validator \
  --region=us-central1 \
  --max-instances=50 \
  --min-instances=1 \
  --concurrency=80
```

## Database Connection from Cloud Run

The Dockerfile uses the Cloud SQL Auth Proxy approach. Ensure:

1. Your service account has `Cloud SQL Client` role
2. The `--add-cloudsql-instances` flag is set during deployment
3. The `DATABASE_URL` includes the Cloud SQL proxy path: `/cloudsql/PROJECT_ID:REGION:INSTANCE_NAME`

## Static Files and Media

Cloud Run instances are ephemeral, so:

1. **Static files** are collected during Docker build (in `Dockerfile`)
2. **Media files** should be stored in Google Cloud Storage (GCS)

To use GCS for media:

```bash
# Install django-storages
pip install django-storages google-cloud-storage

# Add to requirements.txt
echo "django-storages>=1.14.0" >> requirements.txt
echo "google-cloud-storage>=2.10.0" >> requirements.txt
```

Configure in `settings.py`:

```python
if not DEBUG:
    DEFAULT_FILE_STORAGE = "storages.backends.gcloud.GoogleCloudStorage"
    GS_BUCKET_NAME = os.getenv("GS_BUCKET_NAME", "your-bucket-name")
    MEDIA_URL = f"https://storage.googleapis.com/{GS_BUCKET_NAME}/"
```

## Monitoring and Logging

### Cloud Logging

```bash
# View real-time logs
gcloud run services logs read gtm-validator --region=us-central1 --follow

# Search logs
gcloud run services logs read gtm-validator \
  --region=us-central1 \
  --filter="ERROR"
```

### Cloud Monitoring

1. Go to Cloud Console > Monitoring
2. Create a dashboard for:
   - Request latency
   - Error rates
   - CPU and memory usage
   - Database connection pool

## Troubleshooting

### Database Connection Issues

```bash
# Test Cloud SQL connection
gcloud cloud-sql-proxy PROJECT_ID:us-central1:gtm-db --dry-run

# Check service account permissions
gcloud projects get-iam-policy PROJECT_ID \
  --flatten="bindings[].members" \
  --format='table(bindings.role)' \
  --filter="bindings.members:gtm-cloud-run*"
```

### Static Files Not Loading

```bash
# Rebuild and redeploy
docker build --no-cache -t gcr.io/${PROJECT_ID}/gtm-validator:latest .
docker push gcr.io/${PROJECT_ID}/gtm-validator:latest

gcloud run deploy gtm-validator \
  --image=gcr.io/${PROJECT_ID}/gtm-validator:latest \
  --region=us-central1
```

### Memory/Timeout Issues

Increase resource allocation:

```bash
gcloud run services update gtm-validator \
  --region=us-central1 \
  --memory=1Gi \
  --cpu=2 \
  --timeout=300
```

## Rollback

```bash
# View revision history
gcloud run revisions list --service=gtm-validator --region=us-central1

# Rollback to previous revision
gcloud run services update-traffic gtm-validator \
  --region=us-central1 \
  --to-revisions=REVISION_NAME=100
```

## Cost Optimization

1. Set `--min-instances=0` for development/staging
2. Use smaller instances (512Mi memory) if possible
3. Monitor Cloud SQL spending and scale down if needed
4. Consider Cloud Run's free tier: 2M requests/month + 360k GB-seconds/month

## Next Steps

1. Set up a custom domain
2. Configure CDN (Cloud CDN) for static files
3. Set up CI/CD with Cloud Build
4. Configure scheduled tasks with Cloud Tasks or Cloud Scheduler
5. Set up monitoring and alerts
