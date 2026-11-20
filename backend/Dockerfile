# syntax=docker/dockerfile:1.7
# One backend image, several entrypoints: api (default CMD), worker, persister, migrate.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.13.5

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------- build stage
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# 1) dependencies only (cached until pyproject/uv.lock change)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project
# 2) the project itself, installed non-editable into the venv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# -------------------------------------------------------------- runtime stage
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="divert-backend" \
      org.opencontainers.image.description="DIVERT api / worker / persister / migrate"
RUN groupadd --system --gid 10001 divert \
 && useradd --system --uid 10001 --gid divert --home-dir /app --shell /usr/sbin/nologin divert
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DIVERT_LOG_JSON=true
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=divert:divert alembic.ini ./
USER 10001:10001
EXPOSE 8000
# Liveness of the api entrypoint; compose/k8s override or disable it for other roles.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "divert.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
