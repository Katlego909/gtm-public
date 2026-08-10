#!/bin/sh
set -e

# If a command was passed to the container, run it instead of the server.
# The Cloud Build migrate step runs this image as `python manage.py migrate`;
# without this it would be ignored and Gunicorn would boot (a server that never
# exits), hanging the build. Cloud Run starts with no args (Dockerfile CMD []),
# so it falls through to Gunicorn as before.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Start Gunicorn immediately
echo "Starting Gunicorn on port ${PORT:-8080}..."
exec gunicorn --bind 0.0.0.0:${PORT:-8080} \
    --workers 4 \
    --threads 2 \
    --worker-class gthread \
    --worker-tmp-dir /dev/shm \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    gtm_validator.wsgi:application
