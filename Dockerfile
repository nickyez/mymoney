# syntax=docker/dockerfile:1

# ---- Stage 1: build the frontend ----
FROM node:20-bookworm-slim AS frontend

WORKDIR /build
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci

COPY web/frontend/ ./
RUN npm run build


# ---- Stage 2: runtime ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Backend code (app.py, auth.py, db.py, etc.)
COPY web/backend/ ./web/backend/

# Built frontend from stage 1
COPY --from=frontend /build/dist ./web/frontend/dist

# Root-level helpers the backend imports (tracker, daemon)
COPY tracker.py tracker_daemon.py ./

# Non-root user for runtime
RUN useradd --create-home --uid 1000 mymoney \
    && mkdir -p /app/data \
    && chown -R mymoney:mymoney /app
USER mymoney

ENV EXPENSE_DB=/app/data/transactions.db

EXPOSE 8017

# uvicorn must run from the backend directory so `app:app` resolves
WORKDIR /app/web/backend
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8017"]
