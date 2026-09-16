**Uploads now have a per-tenant ceiling and a retention sweep.** `MAX_ATTACHMENT_BYTES`
capped one upload and nothing capped how many — the same gap that produced
`documents_max_per_tenant`, because a per-request cap is not a per-tenant cap. The security
review of `/files` named this as the condition on granting `files:write` to an untrusted
tenant, and it was shipped twice without it, written into the roadmap rather than assumed.

`FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT` (256 MiB, `0` disables) bounds what one tenant may
store. Bytes rather than a count, because the resource being protected is disk — and with
each upload already capped at 600 KiB, a count ceiling is just this number divided by that
one with worse failure text. An upload over the line answers **409**, not 413: the request
is a fine size and the account is full, and a 413 sends the caller off to shrink an image
that was never the problem. It is checked before the object is written, so a refusal leaves
nothing behind.

`FELIX_ATTACHMENT_RETENTION_DAYS` (`0`, keep forever) lets the nightly sweep collect old
uploads. `attachments/` had joined `artifacts/` as an object-store prefix nothing ever
collected, so on the default `fs` backend a tenant's uploads accumulated against the same
disk that holds artifact spill and manifest storage. The default stays "keep forever":
deleting caller data on a timer is an operator's decision, and a thread can reference an
attachment long after it was uploaded.

Both need something the object store cannot give them. The `ObjectStore` Protocol has no
`list` — deliberately, since S3 and GCS charge for it and the filesystem backend would walk
a directory tree per request — so nothing could count what a tenant had stored or find what
was old enough to drop. Migration `0016` adds an `attachments` ledger beside the bytes,
with the same tenant RLS policy as every other tenant table.

The bytes remain the system of record and the ledger can drift from them, so the ordering is
chosen rather than incidental: the row is written **before** the object and deleted **after**
it. Every way this can be interrupted therefore leaves the same shape — a row whose bytes may
not exist — which over-counts, is visible to an operator, and is collected by the sweep. The
opposite order is unrecoverable in both directions: bytes with no row are invisible to the
count *and* to the sweep, because both read rows, and it would also make the quota fail open,
since a total that never grows never refuses anything. Existing uploads are not backfilled, because a backfill would have to
list the object store, which is the operation this table exists because we lack; they are
invisible to the quota and the sweep, exactly as they were before.
