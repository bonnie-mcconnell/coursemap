# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY coursemap/ ./coursemap/
COPY datasets/  ./datasets/

# ── Runtime ────────────────────────────────────────────────────────────────────
ENV HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# Create data directory for plan store
RUN mkdir -p /app/data

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')"

CMD ["sh", "-c", "uvicorn coursemap.api.server:app --host $HOST --port ${PORT:-8080} --no-access-log"]
