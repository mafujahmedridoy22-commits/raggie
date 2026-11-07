# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Build and runtime MUST share the same Python base: the virtualenv built in the
# first stage points at that interpreter.
ARG PYTHON_IMAGE=python:3.13-slim

# Pinned uv release (bump deliberately, together with the lockfile tooling).
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# ── build stage: install exactly what uv.lock pins ───────────
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependency layer: rebuilt only when the lockfile changes. --frozen fails the
# build if pyproject.toml and uv.lock disagree instead of re-resolving.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Project layer: installed non-editable so the runtime image needs no sources.
# --no-cache: uv keys its cached build of a local project on pyproject.toml
# only, so a cache mount here would reinstall a stale wheel after source edits.
COPY raggie_workers ./raggie_workers
RUN uv sync --frozen --no-dev --no-editable --no-cache

# ── runtime stage ────────────────────────────────────────────
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"
WORKDIR /app

# Tesseract (Turkish + English) powers the OCR fallback for image/scanned PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-tur tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 raggie

COPY --from=build /app/.venv /app/.venv

USER raggie
EXPOSE 9100
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/healthz').status==200 else 1)"]
# WORKER_ROLE (ingestion | eval | scheduler) selects the loop at runtime.
CMD ["python", "-m", "raggie_workers.main"]
