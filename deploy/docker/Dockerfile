# Felix multi-stage image — CPython 3.14 + Granian.
#
# Base images are pinned by tag AND digest so a rebuild is reproducible and a
# retagged upstream image cannot change what ships. Dependabot updates the
# digests; refresh by hand with:
#   docker buildx imagetools inspect <image>:<tag>   # take the top-level Digest
#
# Stock CPython — not free-threaded 3.14t, not PyPy/GraalPy. The request path is
# I/O-bound (model calls, Postgres, Valkey, object store), so a faster interpreter
# buys nothing, while granian, pydantic-core, asyncpg and the heavy extras
# (Playwright, sentence-transformers, DuckDB, Presidio) are native extensions built
# against the CPython ABI. Staying on it keeps every install a prebuilt cp314 wheel,
# which UV_PYTHON_DOWNLOADS=never below depends on. Parallelism comes from Granian
# worker processes (FELIX_WORKERS), not threads, so the GIL is not the constraint.
#
# Default image is LEAN (no aws/gcp/embeddings extras) for small VMs.
# Build with extras: docker build --build-arg FELIX_EXTRAS="aws,gcp" .

FROM ghcr.io/astral-sh/uv:0.12@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder
COPY --from=uv /uv /uvx /bin/

ARG FELIX_EXTRAS=""

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# `psycopg[c]` compiles against the system libpq instead of vendoring one, so the builder
# needs headers and a compiler. Neither reaches the runtime stage: only `/app` is copied
# forward, and the runtime installs `libpq5` alone.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev; \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY packages ./packages
COPY apps ./apps
COPY manifests ./manifests
COPY migrations ./migrations
COPY alembic.ini ./

# Core install only. Optional: FELIX_EXTRAS=aws,gcp,otel,temporal,warehouse (comma-separated)
# Always --locked: the image must be built from the committed uv.lock, the same
# resolution CI tested. --locked asserts the lock still matches pyproject and
# fails the build if it does not; --frozen would only skip re-locking, and would
# happily build from a lock that no longer matches. Run `uv lock` and commit it.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
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

# The image ships `psycopg[c]`, not `psycopg[binary]`, and this is the one place the two
# diverge: `pyproject.toml` keeps `[binary]` so that installing Felix -- or contributing to
# it -- needs no compiler and no libpq headers, which is the difference between `pip install
# felix-harness` working everywhere and working where a toolchain happens to be.
#
# The image cannot afford the same convenience. `psycopg[binary]`'s manylinux wheels vendor
# libpq's whole dependency chain and declare it in an auditwheel SBOM: RHEL 8 builds of
# pcre2, krb5, ldap, sasl, selinux, and OpenSSL 1.1.1k. `apt-get upgrade` cannot reach inside
# a wheel, so those ship at whatever version the wheel was built against and stay there --
# which is how the 0.3.0 release scan failed on arm64 while amd64 passed, the two wheels
# having repaired different library sets.
#
# `psycopg[c]` links the system libpq, so every one of those becomes a Debian package that
# `apt-get upgrade` above already patches. Done as an explicit swap rather than by changing
# the dependency, so `--locked` still governs everything else in the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    version="$(uv pip show psycopg | awk '/^Version:/ {print $2}')"; \
    uv pip uninstall psycopg-binary; \
    uv pip install --no-cache "psycopg[c]==${version}"; \
    ! uv pip show psycopg-binary >/dev/null 2>&1

FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS runtime

ENV UV_CACHE_DIR=/tmp/uv \
    UV_NO_SYNC=1 \
    FELIX_DATA_DIR=/data \
    FELIX_HOST=0.0.0.0 \
    FELIX_PORT=8080 \
    FELIX_OBJECT_STORE=fs \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /app /app

# Patch OS packages, then drop pip: the venv is built by uv in the builder
# stage and copied whole, so the runtime needs neither pip nor its vendored
# libraries (which carry their own CVEs). Both shrink the attack surface of
# what actually ships.
RUN set -eux; \
    apt-get update; \
    apt-get upgrade -y --no-install-recommends; \
    apt-get install -y --no-install-recommends libpq5; \
    rm -rf /var/lib/apt/lists/*; \
    python -m pip uninstall -y pip 2>/dev/null || true; \
    rm -rf /usr/local/lib/python3.14/site-packages/pip \
           /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.14/ensurepip

# `/app` stays owned by root. The runtime user needs to *read* the venv and the bundled
# manifests, never to write them — and under FELIX_MANIFEST_SOURCE=bundled those manifests
# are the only definition of every agent's prompt, tools, egress targets and inbound auth.
# Owning them would mean any arbitrary-write primitive in the process rewrites the agents it
# is running, which makes "changing an agent means shipping an image" a convention rather
# than a property. Only /data and /workspace are writable.
#
# /workspace is created here, owned by the runtime user, so that the named `felix-workspace`
# volume compose mounts on it is seeded with that ownership: Docker initialises a new, empty
# named volume from the image's directory at the mount point, ownership included, and with no
# such directory the mount point comes up owned by root and every workspace write fails with
# `Errno 13` — which is what a bind-mounted host directory already did on the reference
# deployment. The builder image does the same for its own workspace volume.
RUN mkdir -p /data /workspace \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin felix \
    && chown -R felix:felix /data /workspace

USER felix
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)"

CMD ["felix-api"]
