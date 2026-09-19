# The builder image — the Felix that builds Felix.
#
# Two trees, deliberately. The image's own Felix runs from /app, built by deploy/docker/Dockerfile
# from the commit that built this image: that is the harness running the loop. /workspace is a
# separate clone the agent edits and runs the gates in, with its own venv: that is the harness
# being edited. `write_file` and the shell tool touch the second; a restart never boots the
# first from it. Keeping them apart is what makes "Felix changed a file" and "Felix is running
# changed code" two different events.
#
# Built FROM the lean image, so `make up-self` builds `felix:latest` first (the Makefile does).
# Adds only what the gates need on the host the shell tool execs on: git, make, uv. No cloud
# SDKs, no Docker socket, no credentials — deploy/GOVERNANCE.md "Shell tools" says why the
# host is the boundary and what it therefore must not hold.

ARG BASE_IMAGE=felix:latest
FROM ghcr.io/astral-sh/uv:0.12@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

FROM ${BASE_IMAGE}
USER root
COPY --from=uv /uv /uvx /usr/local/bin/
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git make ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /workspace; \
    chown felix:felix /workspace
COPY --chown=felix:felix --chmod=755 deploy/docker/self-entrypoint.sh /usr/local/bin/felix-self-entrypoint
USER felix
# uv's cache and the workspace venv live inside the workspace volume, so a first boot's
# sync is paid once and the running image stays read-only apart from /data.
ENV UV_CACHE_DIR=/workspace/.uv-cache \
    UV_PYTHON_DOWNLOADS=never \
    FELIX_WORKSPACE_ROOT=/workspace
ENTRYPOINT ["felix-self-entrypoint"]
CMD ["felix-api"]
