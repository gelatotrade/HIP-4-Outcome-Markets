FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt ./
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY README.md ./

ENV DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8050 \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app

EXPOSE 8050

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8050/healthz || exit 1

# gunicorn for production; thread workers cope with Dash's blocking callbacks.
CMD ["gunicorn", "--bind=0.0.0.0:8050", "--workers=1", "--threads=4", \
     "--timeout=60", "--access-logfile=-", "--error-logfile=-", \
     "src.app:server"]
