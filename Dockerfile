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

# Collect static files
RUN python manage.py collectstatic --noinput

# Debug: List collected static files
RUN ls -R /app/staticfiles

# Set entrypoint
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Cloud Run requires listening on 0.0.0.0:8080
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Run gunicorn with proper signal handling for Cloud Run
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--threads", "2", "--worker-class", "gthread", "--worker-tmp-dir", "/dev/shm", "--max-requests", "1000", "--max-requests-jitter", "100", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "gtm_validator.wsgi:application"]
