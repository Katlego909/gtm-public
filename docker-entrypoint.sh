#!/bin/sh
set -e

# Wait for database to be ready
echo "Waiting for database to be ready..."
python -c "
import os
import sys
import django
from django.db import connections
from django.db.utils import OperationalError
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
django.setup()
conn = connections['default']
try:
    conn.ensure_connection()
except OperationalError:
    sys.exit(1)
" || (echo "Database not ready, retrying..." && sleep 2 && exec "$0")

# Run migrations
echo "Running database migrations..."
python manage.py migrate --noinput

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Start Gunicorn dynamically using the PORT env var
echo "Starting Gunicorn on port ${PORT:-8080}..."
exec gunicorn --bind 0.0.0.0:${PORT:-8080} 
    --workers 4 
    --threads 2 
    --worker-class gthread 
    --worker-tmp-dir /dev/shm 
    --max-requests 1000 
    --max-requests-jitter 100 
    --timeout 120 
    --access-logfile - 
    --error-logfile - 
    gtm_validator.wsgi:application
