# Felix multi-stage image — CPython 3.14 + Granian.
# Default image is LEAN (no aws/gcp/embeddings extras) for small VMs.
# Build with extras: docker build --build-arg FELIX_EXTRAS="aws,gcp" .

FROM ghcr.io/astral-sh/uv:0.9 AS uv

FROM python:3.14-slim AS builder
COPY --from=uv /uv /uvx /bin/

ARG FELIX_EXTRAS=""

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY packages ./packages
COPY apps ./apps
COPY manifests ./manifests
COPY migrations ./migrations
COPY alembic.ini ./

# Core install only. Optional: FELIX_EXTRAS=aws,gcp,otel (comma-separated)
# Always --locked: the image must be built from the committed uv.lock, the same
# resolution CI tested. --locked asserts the lock still matches pyproject and
# fails the build if it does not; --frozen would only skip re-locking, and would
# happily build from a lock that no longer matches. Run `uv lock` and commit it.
RUN set -eux; \
    if [ -z "${FELIX_EXTRAS}" ]; then \
      uv sync --locked --no-dev; \
    else \
      extras="$(echo "${FELIX_EXTRAS}" | tr ',' ' ')"; \
      set --; \
      for e in ${extras}; do \
        set -- "$@" --extra "${e}"; \
      done; \
      uv sync --locked --no-dev "$@"; \
    fi

FROM python:3.14-slim AS runtime

ENV UV_CACHE_DIR=/tmp/uv \
    UV_NO_SYNC=1 \
    FELIX_DATA_DIR=/data \
    FELIX_HOST=0.0.0.0 \
    FELIX_PORT=8080 \
    FELIX_OBJECT_STORE=fs \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /app /app

RUN mkdir -p /data \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin felix \
    && chown -R felix:felix /app /data

USER felix
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)"

CMD ["felix-api"]
