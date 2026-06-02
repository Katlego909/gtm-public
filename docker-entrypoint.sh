#!/bin/sh
set -e

# Wait for database to be ready (if using Cloud SQL Proxy)
if [ -n "$DATABASE_URL" ]; then
    echo "Waiting for database to be ready..."
    python -c "
import os
import sys
import django
from django.db import connection
from django.db.utils import OperationalError

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')

max_attempts = 30
for attempt in range(max_attempts):
    try:
        django.setup()
        connection.ensure_connection()
        print('Database is ready!')
        sys.exit(0)
    except OperationalError:
        if attempt < max_attempts - 1:
            print(f'Database not ready, attempt {attempt + 1}/{max_attempts}, waiting...')
            __import__('time').sleep(2)
        else:
            print('Database failed to become ready')
            sys.exit(1)
"
fi

# Run migrations (with timeout to prevent hanging)
echo "Running database migrations..."
timeout 60 python manage.py migrate --noinput || echo "Migrations timed out or failed, continuing startup..."

# Load GTM defaults (questions, categories, recommendation bands, tools) - optional, skip on timeout
echo "Loading GTM defaults..."
timeout 30 python manage.py load_gtm_defaults || echo "Load defaults timed out, continuing startup..."

# Collect static files already done in build, skip here to save startup time
# echo "Collecting static files..."
# python manage.py collectstatic --noinput 2>/dev/null || true

echo "Starting Gunicorn..."
exec "$@"
