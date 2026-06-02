#!/bin/sh

# Start Gunicorn immediately - skip all database operations at startup
# Migrations will run when needed (on first request) or can be run manually
echo "Starting Gunicorn..."
exec "$@"
