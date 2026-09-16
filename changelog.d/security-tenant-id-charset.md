**A tenant id was validated as a thread-id prefix and used as a path segment.**
`assert_valid_tenant_id` rejected `:` and `#` — the delimiters the `{tenant}:{suffix}`
thread-id rule needs — plus leading and trailing whitespace, and nothing else. But a tenant
id is also interpolated into object-store keys (`artifacts/{tenant}/…`,
`workspace/{tenant}/…`, `skills/{tenant}/…`, `manifests/{tenant}/…`), into idempotency
keys, and into every log record. Those grammars have their own separators, and
`acme/../other` and `acme\nWARNING  all clear` both passed. Under a claim-mode JWT verifier
the tenant id comes from a token claim, which on Cognito is frequently user-writable.

Nothing was exploitable through it, and the reason is worth stating rather than implying a
breach. Outside `development`, a claim-mode verifier with an empty `FELIX_ALLOWED_TENANTS`
refuses to start, so the attacker-writable claim has to match an allowlist entry exactly.
Behind that, `storage/fs.py` re-validates every key segment and re-checks containment with
`relative_to`, S3/GCS keys are literal so `..` is a character rather than a parent, the RLS
GUC is bound as a parameter and compared for equality, and `LogIdsFilter` already escapes
the `tenant_id` log *field*.

What was unescaped is a tenant id interpolated into a log *message*, which four sweeps in
the worker did (`jobs/scheduler.py`, `jobs/anomaly.py`, `jobs/continuous_eval.py`); those
now use `loggable()` like the rest of the repo.

The tenant id is now held to the same rule `storage/fs.py` applies to a key segment, using
the same expression: letters, digits, `.`, `_`, `-`, and never `.` or `..` alone. One
definition of a safe segment rather than two that drift. **A tenant id containing any other
character is now rejected at authentication** — the shapes issuers actually emit (slugs,
UUIDs, domains, `org_`-style ids) are unaffected; an email address used as a tenant id is
the one shape that stops working.
