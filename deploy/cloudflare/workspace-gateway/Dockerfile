# The sandbox a workspace scope's files live in. Python for the `felix-fs` helper, which is all
# the gateway runs inside it; the Durable Object starts it with the internet off and an empty
# environment.
FROM --platform=linux/amd64 python:3.14-slim-trixie

# The helper `DirectoryBackup` runs to move /workspace to and from R2. Its tag must match the
# @cloudflare/sandbox version in package.json.
# git for a thread's repository (cloned through the github.com intercept, `src/github.ts`), and the
# system CA bundle; the intercept's own CA is trusted per command (GIT_SSL_CAINFO), not baked in.
# tini as PID 1: a background command's supervisor detaches from the helper that started it and is
# reparented to PID 1 when it ends, and `sleep` would leave every one a zombie.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=docker.io/cloudflare/sandbox:1.0.0 /usr/local/bin/sandbox-shim /usr/local/bin/sandbox-shim
COPY helper/felix_fs.py /opt/felix-fs/felix_fs.py
# No /workspace here. A restore swaps a new directory in by rename, and overlayfs refuses to rename
# over a directory that comes from an image layer (EXDEV). The Durable Object makes it at start, or
# the restore does, so it is always in the Container's own writable layer.

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
