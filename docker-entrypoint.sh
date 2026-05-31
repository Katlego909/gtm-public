#!/bin/bash
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

# Run migrations
echo "Running database migrations..."
python manage.py migrate --noinput

# Collect static files (in case they weren't collected during build)
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear 2>/dev/null || true

echo "Starting Gunicorn..."
exec "$@"
