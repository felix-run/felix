# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-08-22

### Added

- Initial public release of **Felix** — self-hostable managed agents harness
  (`apiVersion: felix/v1`).
- Surfaces: `/chat`, OpenAI-compatible `/v1`, A2A, MCP, management APIs.
- Lean Docker Compose (api, worker, **scheduler**, Postgres+pgvector, Valkey)
  with optional MinIO (`--profile full`).
- Helm chart with PVC support, consumer shared secret, and scheduler container.
- Durable fibers, audit spill to DuckDB (optional), JWT/api_key auth, plugins seam.
- CLI: `migrate`, `eval`, `mint-jwt`, `bundle-manifests`, `doctor`, `version`.

[0.1.0]: https://github.com/felix-run/felix/releases/tag/v0.1.0
