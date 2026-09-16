**A manifest can now say its declared skills are the whole set.**
`spec.skills_declared_only: true` loads only the names in `spec.skills`; anything else on
the host stays out of the catalogue and out of the model's prompt.

Without it — the default, and unchanged — `load_manifest_skills` seeds every skill in the
bundled directory and in `FELIX_SKILLS_DIR` before it resolves a single ref, so `spec.skills`
adds to a host-wide library rather than restricting one. A manifest declaring one skill
compiles a catalogue holding every skill on the host, and all of them are offered to the
model. That was surfaced by the `/skills` routes and recorded as a question rather than
fixed, because it is a reasonable design and it is what every stored manifest was written
against.

It is worth being able to turn off because a skill body is appended to the system prompt.
An ambient skill is therefore a prompt fragment the manifest never named — the one
prompt-shaping input `pin_compile` cannot cover, since the hash is over the manifest while
the drift is on the host's disk. A manifest that has to be reviewable can now enumerate
every instruction its agent may load; one using the host as a shared library carries on as
before.

**Opt-in on purpose.** Narrowing by default would change behaviour for every manifest
already in Postgres, which this repo's own rule says needs a migration rather than a
reinterpretation — and a stored manifest relying on an ambient skill would start answering
differently with nothing in its own text having changed.

`GET /skills/{manifest}` reports `declared_only` alongside the per-skill `declared` flag, so
the two readings are distinguishable from outside. Restricting does not break resolution: a
declared name still resolves against the bundled directory, so it keeps its body rather than
degrading to an empty placeholder.

Separately, `spec.skills` gained the `max_length` every other ref list already had. Each ref
can cost an object-store lookup at compile, so an unbounded list was an unbounded fan-out.
Naming it plainly, because this repo's own note says a narrowed field has no compat
mechanism the way a removed one does: a stored manifest with more than 64 skill refs stops
validating, and since the store is read ahead of bundled YAML it goes dead rather than
falling back. No such manifest is plausible at that bound, but the precedent should be
visible rather than rediscovered.

**Upgrading.** Adding any field to `spec` changes every manifest's content hash, because
`manifest_content_hash` dumps the model without `exclude_defaults` — so a manifest that
never mentions `skills_declared_only` still hashes differently after this deploy. Two
consequences, both fail-closed and both one-time:

- A thread pinned under `governance.pin_compile: true` raises `ManifestDriftError` on its
  next turn, surfaced as a client error, with nothing in the manifest having changed.
- `durability/fibers.py` *forces* pinning for any fiber carrying stored auth regardless of
  the manifest's own setting, so a durable fiber enqueued before the deploy fails at resume
  — for every manifest, not only pinned ones.

Drain in-flight durable fibers across this deploy, and expect pinned threads to need
re-pinning. `manifests/governed.yaml` sets `pin_compile: true` *and* is edited here, so
threads pinned to it are affected on both counts; the README already records that editing a
bundled manifest is drift by design.

The durable fix is `exclude_defaults=True` in `manifest_content_hash`, which would make the
hash stable against every future defaulted addition and lose nothing — a manifest that
writes a field's default compiles identically to one that omits it. It churns every hash
once, so it belongs in its own change rather than riding along here.
