#!/bin/sh
set -e

# Collect static files during startup (non-fatal)
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear 2>/dev/null || true

# Run migrations with timeout (non-blocking, best effort)
echo "Running database migrations..."
timeout 30 python manage.py migrate --noinput 2>/dev/null || true

# Start Gunicorn immediately on the PORT specified by Cloud Run
PORT=${PORT:-8080}
echo "Starting Gunicorn on 0.0.0.0:${PORT}..."
exec gunicorn \
    --bind 0.0.0.0:${PORT} \
    --workers 4 \
    --threads 2 \
    --worker-class gthread \
    --worker-tmp-dir /dev/shm \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    gtm_validator.wsgi:application
