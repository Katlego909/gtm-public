# Local Docker Testing Guide

This guide helps you test the Docker container locally before deploying to Cloud Run.

## Prerequisites

- Docker Desktop installed and running
- Docker Compose (usually included with Docker Desktop)
- Git repository cloned

## Quick Start

### 1. Build and Run with Docker Compose

```bash
# Build the Docker image
docker-compose build

# Start the services
docker-compose up -d

# Run migrations (if not already run by the container)
docker-compose exec web python manage.py migrate

# Create a superuser for admin access
docker-compose exec web python manage.py createsuperuser
```

### 2. Access the Application

- **Application**: http://localhost:8000
- **Admin Panel**: http://localhost:8000/admin
- **Health Check**: http://localhost:8000/health/

### 3. View Logs

```bash
# View all logs
docker-compose logs -f

# View web service logs only
docker-compose logs -f web

# View database logs only
docker-compose logs -f db
```

### 4. Database Access

Connect to PostgreSQL directly:

```bash
# Using psql
psql -h localhost -U gtm_user -d gtm_validator

# Password: gtm_password
```

Or via Docker:

```bash
docker-compose exec db psql -U gtm_user -d gtm_validator
```

## Testing Different Scenarios

### Test Database Connection

```bash
# Access the web container shell
docker-compose exec web python manage.py shell

# In the Python shell:
from django.db import connections
db = connections['default']
db.ensure_connection()
print("Database connected successfully!")
```

### Test Static Files Collection

```bash
docker-compose exec web python manage.py collectstatic --noinput
```

### Test Migrations

```bash
# Create a new migration
docker-compose exec web python manage.py makemigrations

# Run migrations
docker-compose exec web python manage.py migrate

# Check migration status
docker-compose exec web python manage.py showmigrations
```

### Load Initial Data

If you have fixtures:

```bash
docker-compose exec web python manage.py loaddata fixture_name
```

## Stopping and Cleaning Up

### Stop Services

```bash
# Stop containers (preserves data)
docker-compose stop

# Stop and remove containers
docker-compose down

# Remove everything including volumes (WARNING: deletes database)
docker-compose down -v
```

### Rebuild After Code Changes

```bash
# Rebuild the image after requirements.txt changes
docker-compose build --no-cache

# Restart services
docker-compose down
docker-compose up -d
```

## Testing Environment Variables

To test with different environment variables, create a `.env` file:

```bash
DEBUG=False
DJANGO_SECRET_KEY=test-secret-key-with-at-least-50-chars
ALLOWED_HOSTS=localhost,127.0.0.1,web
DATABASE_URL=postgresql://gtm_user:gtm_password@db:5432/gtm_validator
GCP_PROJECT_ID=test-project
SECURE_SSL_REDIRECT=False
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
```

Then update `docker-compose.yml` to use:

```yaml
env_file:
  - .env
```

## Troubleshooting

### Database Won't Connect

```bash
# Check if database service is running
docker-compose ps

# Check database logs
docker-compose logs db

# Restart database service
docker-compose restart db
```

### Port Already in Use

Change port mappings in `docker-compose.yml`:

```yaml
services:
  web:
    ports:
      - "8001:8000"  # Use 8001 instead of 8000
  db:
    ports:
      - "5433:5432"  # Use 5433 instead of 5432
```

### Build Fails with Python Dependencies

```bash
# Clear Docker cache
docker system prune -a

# Rebuild
docker-compose build --no-cache
```

### Permission Denied on docker-entrypoint.sh

```bash
# Make script executable
chmod +x docker-entrypoint.sh

# Rebuild
docker-compose build --no-cache
```

## Running Management Commands

```bash
# Format: docker-compose exec web python manage.py [command]

# Load default data
docker-compose exec web python manage.py load_gtm_defaults

# Create cache table
docker-compose exec web python manage.py createcachetable

# Collect static files
docker-compose exec web python manage.py collectstatic --noinput --clear
```

## Testing with Different Django Settings

To test production settings locally:

```bash
# Create a settings file for testing
# settings_test.py with DEBUG=False, ALLOWED_HOSTS=['*'], etc.

# Update docker-compose.yml:
environment:
  DJANGO_SETTINGS_MODULE: gtm_validator.settings_test
```

## Performance Testing

### Check Container Resource Usage

```bash
# Monitor CPU, memory, and network
docker stats
```

### Load Testing with Locust

```bash
# Install locust
pip install locust

# Create a locustfile.py in the project root
# Then run:
locust -f locustfile.py -H http://localhost:8000
```

## Verifying Docker Configuration for Cloud Run

Before deploying to Cloud Run, verify:

1. **Health Check Works**
   ```bash
   curl http://localhost:8000/health/
   ```

2. **Static Files Are Collected**
   ```bash
   docker-compose exec web ls -la /app/staticfiles/
   ```

3. **Database Migrations Run Automatically**
   ```bash
   # Check for migration errors in logs
   docker-compose logs web | grep -i migration
   ```

4. **Environment Variables Load Correctly**
   ```bash
   docker-compose exec web python -c "import os; print(os.getenv('DEBUG'))"
   ```

5. **Port 8000 Is Correct** (Cloud Run uses port 8080)
   - In the Dockerfile, the CMD uses `--bind 0.0.0.0:${PORT}`
   - This respects the PORT environment variable
   - Local testing uses port 8000 in docker-compose.yml

## Debugging Common Issues

### Static Files Returning 404

```bash
# Check static files location
docker-compose exec web python -c "from django.conf import settings; print(settings.STATIC_ROOT)"

# List collected files
docker-compose exec web find /app/staticfiles -type f | head -20
```

### Database Transactions Failing

```bash
# Check database logs
docker-compose logs db

# Test transaction support
docker-compose exec web python manage.py dbshell
# In psql: SELECT VERSION();
```

### Gunicorn Worker Issues

Adjust in Dockerfile if experiencing timeouts:

```dockerfile
# Reduce workers for development
CMD exec gunicorn \
    --bind 0.0.0.0:${PORT} \
    --workers 2 \
    --timeout 120 \
    gtm_validator.wsgi:application
```

## Next Steps

Once you've verified everything works locally:

1. Read [CLOUD_RUN_DEPLOYMENT.md](CLOUD_RUN_DEPLOYMENT.md)
2. Set up your GCP project
3. Build and push to Container Registry
4. Deploy to Cloud Run
