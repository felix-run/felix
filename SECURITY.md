# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `0.1.x` (main) | Yes |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email **blake.bauman@gmail.com** with:

- A short description of the issue
- Steps to reproduce (PoC if available)
- Affected version / commit if known
- Your preferred contact for follow-up

We aim to acknowledge reports within a few business days.

## Scope notes

Felix is self-hosted. Reports involving default-insecure local settings
(`FELIX_AUTH_MODE=none` + `FELIX_ALLOW_INSECURE=true`) are only in scope when
they affect production-oriented defaults (`jwt` / `api_key`, Helm chart, or
documented secure deploy paths).
