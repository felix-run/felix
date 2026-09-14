**`approvals` can now be reclaimed, and `jobs/retention.py` stops claiming it already was.**
That module's docstring listed the table among those "bounded by something else… `approvals`
and `job_runs` go with their run or job". For `approvals` no such binding exists: no foreign
key, no cascade, and nothing anywhere issued a delete against it, so every gate firing left a
row that outlived its run by the life of the deployment. Worse, nothing moves a *timed-out*
row off `pending` — `wait_for_decision` returns a denial and the caller writes nothing back —
so the set a thread-scoped query walks grew monotonically and forever.

`FELIX_APPROVAL_RETENTION_DAYS` (default **0**, keep) sweeps **settled** approvals older than
the TTL, on both backends, under the nightly job.

**Settled means the row can no longer authorize a call**, and that distinction is the whole
design. An approval row is a *permission*: deleting one that could still be returned by
`find_approved` would revoke it silently, from a cron job, days after an operator granted it —
experienced as a tool that used to work and now denies, with nothing in the audit trail saying
why. So the rule is the exact negation of `find_approved`'s own predicate:

- swept: anything not `approved`, plus an `approved` grant whose `expires_at` has passed
- never swept: an unexpired `approved` grant, however old — and one with a **null**
  `expires_at`, because the rule set no `ttl_seconds` and that grant is standing by
  construction

`consumed_at` is deliberately not part of the test. `one_shot` lives on the manifest rule
rather than the row, so a consumed grant still authorizes a tool whose rule is not one-shot.

**Off by default**, matching `FELIX_SESSION_RETENTION_DAYS`: the row is the record of a human
decision on a gated tool, and the `soc2` and `eu_ai_act` profiles lean on it, so an operator
opts in rather than out. Turning it on is also what bounds the pending-row accumulation that
made a durable run's thread-scoped approval query walk a growing set.
