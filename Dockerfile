# ── Stage 1: Builder ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /code

# Install runtime-only system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libgeos-c1v5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ /code/app/
COPY alembic.ini /code/alembic.ini

# Create non-root user
RUN adduser --disabled-password --gecos '' appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
