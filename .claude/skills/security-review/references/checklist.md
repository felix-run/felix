# Felix security checklist

## Tenant isolation
- [ ] Every new query carries `tenant_id`; stores go through `tenant_session()` when RLS is on.
- [ ] Any new `rls_bypass()` has a written justification and is unreachable from request handling.
- [ ] New tables tenant-scoped with an RLS policy mirroring `0006_tenant_rls.py`.
- [ ] Object-store keys and artifact paths are tenant-prefixed.
- [ ] Session/thread ids from the caller are validated against the caller's tenant, never trusted.

## Untrusted content and prompt injection
- [ ] Output from MCP servers, A2A peers, browser tools, queues, and sandboxes is treated as
      attacker-controlled (`_is_untrusted_tool` marks them).
- [ ] Content screening is reachable for the new path; `on_flag` behavior is deliberate.
- [ ] Inbound user turns pass `governance/inbound.py` screening where it applies.
- [ ] Tool descriptions/results cannot inject system-prompt-level instructions unescaped.
- [ ] Skill and manifest content from a tenant cannot alter another tenant's prompt.

## Secrets
- [ ] Credentials resolved via `secret:NAME` / `secrets.py`; nothing inline in a manifest.
- [ ] Every resolved secret is in the masking list (`_collect_secrets` → `apply_secret_masking`).
- [ ] No secret in logs, exception messages, audit rows, usage events, or artifact spill.
- [ ] `FELIX_SECRETS_BACKEND` path (env/file/aws/gcp) fails closed when the secret is missing.
- [ ] `governance.forbid_plaintext_secrets` still rejects the new configuration shape.

## Egress / SSRF
- [ ] Every outbound URL passes the SSRF guard (`security/ssrf.py`): no localhost, link-local,
      metadata endpoints, or private ranges unless explicitly allowed.
- [ ] Plain `http://` only when `environment=development` **and** `allow_insecure=true`.
- [ ] Redirects are not blindly followed to a new host; timeouts and body caps are set.
- [ ] Sandbox/container gateways declare their network needs; no host mounts.

## Auth
- [ ] New management route calls `require_mgmt_scopes` with the narrowest scope.
- [ ] New `/internal/*` route verifies `FELIX_CONSUMER_SHARED_SECRET` in constant time.
- [ ] Manifest `auth.inbound` (schemes, `allow_anonymous`, `required_scopes`) enforced **before**
      the agent is built (`prepare_tenant_invoke`).
- [ ] Nothing relies on `auth_mode=none` behavior being safe in production.
- [ ] Rate limiting has a sensible key for the new surface; body limit is appropriate.

## Execution controls
- [ ] Side-effecting tools sit behind approvals and inside `max_tool_calls` /
      `max_wall_clock_seconds`.
- [ ] Unattended/durable runs cannot self-approve when `allow_unattended: false`.
- [ ] Command screening covers shell-shaped arguments on the new tool.
- [ ] No unbounded loop, unbounded artifact spill, or unbounded fan-out in the new path.

## Data lifecycle
- [ ] Retention (`governance.retention_days`, `jobs/retention.py`) covers new stored data.
- [ ] Audit events emitted for every control that fires and for state-changing management calls.
- [ ] Exported/forked sessions do not leak another tenant's or another user's content.
