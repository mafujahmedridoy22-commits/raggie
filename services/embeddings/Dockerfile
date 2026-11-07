# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Build and runtime MUST share the same Python base: the virtualenv built in the
# first stage points at that interpreter.
ARG PYTHON_IMAGE=python:3.13-slim

# Which pinned local models to bake into the image (comma-separated subset of
# dense,sparse,rerank). Release variants:
#   -local: dense,sparse,rerank (default; no runtime downloads, no egress)
#   -slim:  sparse              (cloud mode: dense/rerank come from an upstream)
ARG EMBEDDINGS_BAKE=dense,sparse,rerank

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
COPY raggie_embeddings ./raggie_embeddings
RUN uv sync --frozen --no-dev --no-editable --no-cache

# ── models stage: download the pinned ONNX snapshots ─────────
FROM ${PYTHON_IMAGE} AS models
ARG EMBEDDINGS_BAKE
ENV PATH="/app/.venv/bin:${PATH}" \
    FASTEMBED_CACHE_PATH=/models \
    HF_HUB_DISABLE_TELEMETRY=1
COPY --from=build /app/.venv /app/.venv
# Each baked model is loaded once, so a broken snapshot fails the build.
RUN mkdir -p /models && python -m raggie_embeddings.bake "${EMBEDDINGS_BAKE}"

# ── runtime stage ────────────────────────────────────────────
# Runs with a read-only root filesystem: the baked models are only read in
# place (FASTEMBED_CACHE_PATH), downloads are off (HF_HUB_OFFLINE; a missing
# model is a clear startup error) and every writable path points under /tmp
# (mount an emptyDir/tmpfs there). Bytecode is precompiled in the venv.
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    FASTEMBED_CACHE_PATH=/home/raggie/.cache/fastembed \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/tmp/huggingface \
    XDG_CACHE_HOME=/tmp/.cache \
    TMPDIR=/tmp
WORKDIR /app

RUN useradd -m -u 10001 raggie

COPY --from=build /app/.venv /app/.venv
COPY --from=models --chown=raggie:raggie /models /home/raggie/.cache/fastembed
# Model license texts (see MODELS.md), shipped in every variant.
COPY licenses/ /app/licenses/

USER raggie
EXPOSE 8002
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('EMBEDDINGS_PORT', '8002')).status==200 else 1)"]
# Listens on EMBEDDINGS_PORT (default 8002).
CMD ["python", "-m", "raggie_embeddings"]
