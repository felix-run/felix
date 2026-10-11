#!/usr/bin/env bash
# From a fresh clone to a working checkout: check the tools, write .env, install, hook up
# pre-commit. Safe to re-run: an existing .env keeps every value except the shipped
# placeholder passwords, which are replaced once with generated ones.
set -euo pipefail
cd "$(dirname "$0")/.."

PLACEHOLDER="change-me-use-openssl-rand-hex-32"
missing=0

need() {  # need <tool> <why> — a hard requirement
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "  ✗ $1 — $2" >&2
    missing=1
  else
    echo "  ✓ $1"
  fi
}
want() {  # want <tool> <why> — only some paths need it
  if command -v "$1" >/dev/null 2>&1; then echo "  ✓ $1"; else echo "  - $1 not found — $2"; fi
}

echo "Tools:"
need uv "install it: https://docs.astral.sh/uv/getting-started/installation/ (it fetches Python 3.14 itself)"
want docker "needed for 'make up' and 'make db'; the test suite runs without it"
want jq "the README's curl examples pipe through it"
[ "$missing" = 0 ] || { echo "Install the tools marked ✗, then re-run 'make bootstrap'." >&2; exit 1; }

secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32; else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Wrote .env from .env.example."
fi

# Replace a placeholder password on one KEY= line. `-i.bak` then rm: the one spelling of an
# in-place edit that both BSD (macOS) and GNU sed accept.
fill() {
  if grep -qE "^$1=$PLACEHOLDER\$" .env; then
    sed -i.bak "s|^$1=$PLACEHOLDER\$|$1=$(secret)|" .env && rm -f .env.bak
    echo "Generated $1 in .env."
  fi
}
fill POSTGRES_PASSWORD
fill MINIO_ROOT_PASSWORD
# A .env copied before FELIX_DATABASE_URL referenced ${POSTGRES_PASSWORD} spells the
# placeholder inside the URL; point it at the variable so the two cannot disagree.
if grep -qE "^FELIX_DATABASE_URL=.*:$PLACEHOLDER@" .env; then
  sed -i.bak "s|:$PLACEHOLDER@|:\${POSTGRES_PASSWORD}@|" .env && rm -f .env.bak
  echo "Pointed FELIX_DATABASE_URL at \${POSTGRES_PASSWORD}."
fi
if grep -qE "^FELIX_S3_SECRET_KEY=$PLACEHOLDER\$" .env; then
  sed -i.bak "s|^FELIX_S3_SECRET_KEY=$PLACEHOLDER\$|FELIX_S3_SECRET_KEY=\${MINIO_ROOT_PASSWORD}|" .env && rm -f .env.bak
fi

echo "Installing (lean core + dev; 'make install-full' adds every extra)..."
uv sync --dev
uv run pre-commit install >/dev/null && echo "pre-commit hook installed."

cat <<'MSG'

Ready. Next:
  make test        the suite, in-memory — no database or model key needed
  make up          the full stack in Docker (api :8080, worker, Postgres, Valkey)
  make db migrate dev   or: Postgres + Valkey in Docker, the API on your machine

A model key goes in .env before the first chat: FELIX_ANTHROPIC_API_KEY or
FELIX_OPENAI_API_KEY (see .env.example for other providers).
MSG
