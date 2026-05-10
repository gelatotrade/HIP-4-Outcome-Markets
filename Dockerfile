FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

# build-essential covers any source-build fallback for scipy/Pillow/etc.
# curl is kept for the HEALTHCHECK probe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        gcc \
        g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install --retries 5 -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY scripts/ ./scripts/

ENV DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8050 \
    LOG_LEVEL=INFO \
    PYTHONPATH=/app

EXPOSE 8050

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8050/healthz || exit 1

# gunicorn for production; thread workers cope with Dash's blocking callbacks.
CMD ["gunicorn", "--bind=0.0.0.0:8050", "--workers=1", "--threads=4", \
     "--timeout=60", "--access-logfile=-", "--error-logfile=-", \
     "src.app:server"]
