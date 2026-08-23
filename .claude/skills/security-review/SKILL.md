---
name: security-review
description: Threat-model-driven security review of Felix changes — tenant isolation and RLS, auth modes and management scopes, the governance and screening pipeline, secret handling and masking, SSRF and outbound egress, sandboxes, approvals, and prompt-injection paths. Use before shipping changes to auth, governance, secrets, tools, or outbound integrations, and when asked for a security review or audit.
allowed-tools: Read Grep Glob Bash(git:*) Bash(rg:*)
---

# Security review

Felix runs untrusted model output against real tools, real credentials, and multi-tenant data.
Rank findings by this threat model:

1. Cross-tenant data access
2. Credential exfiltration (tool output, logs, audit rows, artifact spill)
3. Prompt injection escalating into tool execution
4. SSRF / unintended egress
5. Auth bypass on management or `/internal/*` surfaces
6. Sandbox escape / resource exhaustion

## Control map

| Concern | Code |
|---|---|
| Wrapper stack (ordering *is* the control) | `manifests/builder.py` |
| Auth modes, JWT, middleware | `auth/{middleware,jwt,context}.py` |
| Management scopes | `auth/mgmt.py` — **no-op when `auth_mode=none`** |
| Per-manifest inbound auth | `manifests/inbound_auth.py` |
| Injection / content screening | `governance/{inbound,content_screening}.py` |
| PII | `governance/pii.py` (Presidio, optional extra) |
| SSRF, rate limit, constant-time, at-rest | `security/{ssrf,rate_limit,constant_time,at_rest,expr}.py` |
| Secret refs + masking | `manifests/secret_refs.py`, `secrets.py`, `builder.py:apply_secret_masking` |
| Tenant RLS | `db/session.py` (`tenant_session`, `rls_bypass`), `migrations/versions/0006_tenant_rls.py` |
| Outbound/inbound integrations | `mcp/`, `a2a/peers.py`, `tools/{sandboxes,queues,transports,client_bridge,browser}.py` |

Detailed checklist: [references/checklist.md](references/checklist.md).

## What CI already covers

Do not spend review time on what the `Security` workflow checks on every PR:
CodeQL (`security-and-quality` queries), `pip-audit` over the locked dependency
set including extras, a gitleaks scan of the full history, and Trivy on the
built image. Your job is the half a scanner cannot do — tenant isolation,
control ordering, injection paths, and whether a guard is reachable.

## Method

1. Scope the diff (`git diff HEAD`, untracked files, or the named surface).
2. For each item in the checklist that the diff touches, **grep for the guard you expect** and read
   it. Prove absence before reporting absence — controls are often applied one layer up in the
   wrapper stack.
3. Trace one concrete attack path per finding: attacker-controlled input → the control that fails →
   the impact.
4. Check the test surface: `tests/unit/test_security_hardening.py`, `test_hardening.py`,
   `test_mgmt_rbac.py`, `test_deny_output.py`, `test_queues_stream_screening.py`,
   `test_plan_a2a_secrets.py`.

## Report

Findings by severity (critical / high / medium / low), each with `file:line`, the attack path, and
the remediation. Then list what you checked and found sound, and anything that needs a decision
from the user rather than a code change. Map affected controls back to `deploy/GOVERNANCE.md`.
