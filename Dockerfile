# Multi-stage build for optimized image size
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies - including those needed for various packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libssl-dev \
    libffi-dev \
    python3-dev \
    gcc \
    g++ \
    git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install wheel
RUN pip install --upgrade pip setuptools wheel

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir --no-warn-script-location -r requirements.txt
RUN pip install --user --no-cache-dir gunicorn

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libc6 \
    libssl3 \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Make sure scripts are available in PATH
ENV PATH=/root/.local/bin:$PATH

# Copy project code
COPY . .

# Copy and make entrypoint script executable
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Create necessary directories
RUN mkdir -p /app/staticfiles /app/media

# Collect static files during build. DEBUG=False selects the hashed manifest
# storage production uses; this must fail the build loudly, because a missing
# manifest makes every {% static %} tag raise at request time. The secret key
# is a build-only placeholder (settings refuse DEBUG=False without one); the
# real key comes from Secret Manager at runtime.
RUN DEBUG=False DJANGO_SECRET_KEY=collectstatic-build-only \
    python manage.py collectstatic --noinput --clear

# Set entrypoint
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Cloud Run environment variables
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=gtm_validator.settings

# Default CMD (can be overridden by entrypoint)
CMD []
