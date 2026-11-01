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

# Tailwind stage: compiles theme/static/css/tailwind.css from the templates
# with the standalone CLI (no Node), so production CSS always matches the code
# even if the committed copy is stale. Version + checksum pinned; bump both
# together (and use the same version locally -- see theme/tailwind.config.js).
FROM debian:bookworm-slim AS css
ARG TAILWIND_VERSION=v3.4.17
ARG TAILWIND_SHA256=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSfL -o /usr/local/bin/tailwindcss \
       "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && echo "${TAILWIND_SHA256}  /usr/local/bin/tailwindcss" | sha256sum -c - \
    && chmod +x /usr/local/bin/tailwindcss
WORKDIR /app
# Only what the config scans (templates, Python, theme JS).
COPY templates/ templates/
COPY gtm/ gtm/
COPY dashboard/ dashboard/
COPY theme/ theme/
RUN tailwindcss -c theme/tailwind.config.js -i theme/tailwind.input.css \
    -o theme/static/css/tailwind.css --minify

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
COPY --from=css /app/theme/static/css/tailwind.css theme/static/css/tailwind.css

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
