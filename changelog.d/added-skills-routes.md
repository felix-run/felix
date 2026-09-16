**Agent Skills are reachable over HTTP.** `grep -rn skill apps/api/src/felix_api/routes/`
returned zero: a loader, a catalog, an activation store with its own table and Postgres arm,
and three model-facing tools — none of it answerable to an operator without opening psql. A
surface nothing can reach is inert by this repo's own rule, and skills were the largest
built-but-unreachable subsystem left.

`GET /skills/{manifest}` lists what the manifest can reach and which are active.
`GET /skills/{manifest}/{skill}` returns one, including the body `activate_skill` would hand
the model — that body is appended to the system prompt, so it is prompt content an operator
is accountable for and could not otherwise read. `GET /skills/{manifest}/activations/recent`
says which skill activated on which turn. All three gate on a new `skills:read` scope, kept
separate from `manifests:read` because reading a skill body is reading instructions the
agent will follow, which is a different question from reading the manifest that names it.

Read-only, deliberately: activation is a decision the model makes mid-turn, and the store is
keyed by `(tenant, manifest)` rather than by thread, so an operator toggling from outside
would be writing shared state a run is concurrently reading with no turn to attribute it to.

Two things the routes surface that nothing surfaced before, both found while building them:

- **`spec.skills` adds to a host-wide library rather than restricting one.**
  `load_manifest_skills` seeds every skill in the bundled directory and in
  `FELIX_SKILLS_DIR` before it resolves a single ref, so a manifest declaring one skill
  compiles a catalog holding every skill on the host — and all of them are offered to the
  model. Verified directly: a manifest naming one skill reached seven, including the repo's
  own `felix-architecture` and `felix-contributing`. This may well be the intent, but it is
  not what "declared skills" reads like and nothing said so anywhere. The new `declared`
  field on each item is how the difference becomes legible; the behaviour itself is
  unchanged here and recorded in the roadmap as a question.
- **The audit trail recorded that a skill activated but never which one.** `tool_runner`
  already emits a `tool_call` event for every tool, and its payload carries the tool's name
  and not its arguments — deliberately, since arguments are arbitrary model text and a
  credential in a retained row is how that goes wrong. `skills/tools.py` now emits a
  `skill_activation` event naming the skill, which is safe to record precisely because
  `activate` resolves it against the catalog first: the stored value is a name the host
  declared rather than anything the model typed. A model naming a skill that does not exist
  is recorded too, under its own status, because that is itself worth seeing. Both tools
  resolve first: `deactivate` originally did not, and `activation_store.deactivate` is a list
  filter that neither validates a name nor reports whether anything was removed — so every
  call succeeded and arbitrary model text went into a retained row under `status="ok"`,
  indistinguishable from a real deactivation. Both reviewers found it; the security review
  ranked it the highest finding in the change.

A skill body is redacted before it leaves, for the reason `routes/manifests.py` redacts a
manifest on `manifests:read`: this is the lower scope and an embedded credential must not
ride out on it. Nothing validates SKILL.md frontmatter, and the same bytes reaching the
*model* are already masked by the governance stack — so without this the HTTP route would
have been the only path on which a skill body reached anyone unmasked. The absolute
`Skill.path` is reduced to a basename for the same reason: it is derived from the install
prefix, so returning it told a tenant-scoped caller the container's filesystem layout.

`audit_store.query` gained an optional `manifest_id` filter. The route first over-fetched a
fixed window and narrowed in Python, which is wrong in a way the caller cannot detect: a
tenant whose activations on one manifest exceeded the window got an empty list for another,
identical on the wire to "that manifest has never activated a skill" — and a model calling
`deactivate_skill` in a loop could push a real activation out of view, which is
anti-forensics against the one question the route exists to answer. The filter is four
lines mirroring the `event_type` and `status` filters already there, and adds no clause for
any caller that does not pass it.

Also: `felix.skills.store.clear_memory()`, the test seam every other `memory://` store
already had, wired into the conftest registry that exists to call these. Without it one
test's activation is the next test's starting state, which is how a tenant-isolation
assertion passes alone and fails in a file.
