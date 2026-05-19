FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

# ── api ──────────────────────────────────────────────────────────────────────
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── worker ───────────────────────────────────────────────────────────────────
FROM base AS worker
CMD ["python", "-m", "arq", "service.worker.WorkerSettings"]
