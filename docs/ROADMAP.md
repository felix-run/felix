# Felix roadmap

Living tracker for what to build next. Update status in place; keep items
concrete enough to pick up in a single session.

**Repos:** `felix-run/felix` (harness) · `felix-run/web` (chat-ui + docs)
**Live:** [api.felix.run](https://api.felix.run) · [make.felix.run](https://make.felix.run) · [docs.felix.run](https://docs.felix.run)
**Last reviewed:** 2026-10-10 (completed items folded into HISTORY.md); before that 2026-10-08 (after 0.12.0 rolled to production; the full open-item check was
2026-09-29, after 0.5.0)

Completed waves and what they taught now live in [HISTORY.md](HISTORY.md).

---

## How to use this file

| Status | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress / partial |
| `[x]` | Done (fold into [HISTORY.md](HISTORY.md) on the next tidy pass) |
| `[!]` | Blocked / deferred on purpose |

Pick from **Now** unless a demo needs something from **Next**.

---

## The loop

**Dogfood `contributor.yaml` on real Felix work → fix what breaks → write it down here.**

The program that turns that sentence into rungs — who proposes, who decides, what a ticket must
cite, what Felix may never edit, and the numbers that graduate each rung — is
[SELF.md](SELF.md). This file stays the *what*; that one is the *how*.

This replaces the loop this file carried until 2026-09-02, which read "dogfood float". `float`
was deleted from `felix-run/web` on 2026-08-23 — *"what it actually contributed was a mode, not
a product"* — and the line survived it by ten days. That matters more than a stale link: with
nothing running real work, the only remaining source of tasks was the source tree, and the
harness spent roughly forty commits auditing itself.

Self-audit is not wasted — the mutation audit in [HISTORY.md](HISTORY.md) found two controls
that were silently absent, which no amount of feature work would have surfaced. But it is a
*supplement* to a running workload, not a substitute for one, and it cannot tell you that the
support agent has no way to look anything up.

### Meta-work budget

Two rules, mirrored in `.claude/rules/felix-invariants.md`:

- **One hardening / invariant / audit item per cycle.** Everything else must add user-visible
  capability. Defects found in a *real run* are exempt — that is the loop working.
- **A control may not be added for a capability that does not exist.** This is the rule whose
  absence produced 1,401 lines of `manifests/builder.py` wrapping a calculator.

---

## The finding this cycle turns on

Audited 2026-09-02. Every number re-derived against the tree, not read from a prior note.

| Measure | Value |
|---|---|
| `felix/tools/` — the capability surface | 2,142 lines |
| `.claude/` — scaffolding for the agent that edits Felix | 3,200 lines |
| `manifests/builder.py` — governance wrapping that surface | 1,401 lines |
| Built-in tool registry | 9 tools |
| `skills/` shipped Agent Skills | 5, of which 4 document Felix itself |

The built-in registry in full: `calculator`, `list_dir`, `read_file`, `write_file`,
`edit_file`, `search_files`, `list_skills`, `activate_skill`, `deactivate_skill` — and the three skill tools
are stubs returning `[]` until `builder.py:1251` rebinds them against a catalog.

`manifests/support.yaml` declares `tools: [calculator, list_skills]` — a support agent that
cannot look anything up. `manifests/deep.yaml` declares the same — a deep-research agent that
cannot retrieve. An agent on Felix cannot read a URL, search, query a database, or retrieve a
document. `HttpExecutor` has existed at `tools/transports.py:20` since early on and **no
manifest field constructs it**.

The governance stack is the best-engineered part of the harness and it is guarding almost
nothing. Everything in **Now** follows from that.

---

## Now

### A. Capability surface

First, because everything else governs it.

- [~] **Document retrieval** — the corpus landed: `felix/documents/` (chunking, hybrid store,
      in-memory twin), migration `0010`, conformance against both backends, and `/documents`
      management routes so an operator can ingest, search, inspect and remove. Split from the
      agent-facing half deliberately, on the evidence that the two smaller features in this
      workstream each drew ~7 review findings. The follow-up landed: `spec.document_tools`
      binds a retrieval tool and `support` declares it as `search_docs`. What remains of this
      item is ingesting the Felix docs themselves into a deployment's corpus, which is an
      operations task rather than a harness one. The tool for it landed 2026-10-02:
      `felix ingest-docs <dir> --site-url https://docs.felix.run [--prune]` syncs the felix-web
      pages, one document per page sourced at its public URL so `search_docs` hits can be
      followed with `fetch_docs`. Still to do: run it against the reference deployment (needs a
      `documents:write` key there), and decide whether CI re-syncs on a felix-web docs merge.
      Reuses the `Embedder` seam and `FELIX_MEMORY_EMBEDDER` rather than adding a second
      embedder setting — one embedder per deployment, one vector dimension.
- [~] **Attachments** — split, on the evidence that the two smaller features in the document
      retrieval workstream each drew ~7 review findings where one wide branch would have drawn
      them all at once.
      - Landed: **inline images actually work.** `/v1/chat/completions` takes OpenAI's list of
        content parts, so an SDK can send an image; and the Anthropic wire encodes a `data:` URL
        as a `base64` source instead of putting it in a `url` source, which that API rejects.
        That second one was a live defect rather than a missing feature — both wires had an
        image encoder, and an image reached `gpt-4o` and 400'd on `claude-sonnet`, the default.
        Found by running the encoder, not by reading it.
      - Landed: **the storage half of upload.** `POST /files` / `GET /files/{file_id}`, backed
        by the object store and gated on new `files:read` / `files:write` scopes; tenant from the
        caller's credentials and never from the path, 404 for a malformed reference, server-issued
        uuid4 id. Capped at 600 KiB decoded and to the image types both wires encode — the cap
        sits below `CORE_BODY_LIMIT_BYTES` deliberately, since above it the middleware answers 413
        before the route and hides the real ceiling.
      - Landed: **the `file_id` content block**, resolved to bytes at the wire. Resolution is
        late, per turn, so the session event log keeps the reference rather than the base64 it
        expands to. One correction to this entry as written: the reference does **not** need a
        new field. It rides in the `url` an inline image already uses, as `felix-file://<id>`,
        because `session/types.py` persists and restores `attachments[].url` and both wires read
        it — a parallel `file_id` field would have had to be threaded through each, and the one
        that would have been missed is the session layer, which is the only path a *second* turn
        takes. Nothing would have failed until replay, which is this repo's defect shape exactly.
        `packages/ai` still does no resolving (it may not import `felix`); it parses the part into
        a reference and drops any reference that reaches a wire unexpanded, since that means the
        harness did not do its half.
      - Landed, and it was the stated condition on granting `files:write` to an untrusted
        tenant: **a per-tenant quota and a retention sweep**.
        `FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT` bounds stored bytes (409 over the line, not
        413 — the request is a fine size and the account is full) and
        `FELIX_ATTACHMENT_RETENTION_DAYS` lets the nightly sweep collect old uploads;
        `attachments/` had joined `artifacts/` as a prefix nothing collected. Both needed
        migration `0016`'s ledger, because the `ObjectStore` Protocol has no `list` — so
        nothing could count what a tenant held or find what was old. Bytes rather than a
        count, since each upload is already capped at 600 KiB and the resource is disk.
        The bytes stay the system of record: the row is written before the object and deleted
        after it, so every interruption leaves a row whose bytes may not exist -- visible and
        collectable -- rather than bytes no count can name. Both reviewers caught this the
        other way round on the first pass, where it would have made the quota fail open. Existing uploads are not backfilled — a backfill would need the `list`
        this table exists because we lack.
      - Decided, and the answer made the choice smaller than it looked: resolution sits **after**
        `apply_inbound_screening`. Checked rather than reasoned — `governance/inbound.py:_message_text`
        collects only blocks whose type is `text`, so image content has never reached a screener
        and resolving early would have handed it a block it ignores. So this is not a coverage
        regression; it puts `file_id` images exactly where inline images already were. The real
        item underneath — **image content was not screened at all**, so text rendered inside an
        image was an injection channel on both paths — landed as `content_screening.image_model`:
        a vision model transcribes each user image and the transcript goes through the text
        screeners. Resolution for screening happens inside the control, under the same request
        context, so it reads the bytes the wire will send; the wire's own late resolution is
        unchanged. Remote URLs are unscreenable by construction (the provider fetches them) and
        fall under `on_flag`. Found on the way: a turn beginning `[quarantined]` skipped the model
        and decider scorers, because "already quarantined" was a test of caller-written text.
      - Open, from the security review of image screening:
        - ~~**Replayed history is not screened.**~~ Done: `ScreenedSessionStrategy` screens every
          `render` of a session — a turn's assembly, compaction after a turn, overflow recovery,
          a plugin pattern's — and every way an image got into the log (another manifest, before
          `image_model`, a write-back). On the render, not at a caller: the first version
          screened in `_assemble_messages` only, and the security review found compaction and
          overflow recovery re-render and send history straight to the model. Not at the wire
          either: that runs every step and would screen its own transcriber call. Always
          quarantine, never refuse, since the log is append-only. Each compile wraps the
          strategy it inherits, so a router's screen holds for its children. Verdicts are cached
          beside transcripts, so a replayed image is scored once.
        - Open, small: a flagged replayed image writes an `inbound_screening` audit row (surface
          `history_image`) on every turn the thread lives; deduplicate per thread and image if
          it proves noisy. And `BuildDeps.compiled` memoises a child by name, so a child first
          compiled under a parent with no screen keeps none when another router reuses it in
          the same build — the same memo behaviour `reply_screen` already has.
        - **Input PII does not read image transcripts.** Pixels cannot be redacted, so a match
          could only quarantine the image or refuse the turn.
        - **Screening spend is recorded without a `manifest_id`** when it runs in the route's
          pre-screen, so it sits outside per-manifest usage and `limits.max_cost_usd`. True of
          the text screener already; images make it larger.
      - Follow-up, from the quality review of the quota: **split `felix/attachments.py`**. It is
        ~600 lines doing four jobs — magic-number validation, key and containment rules, the
        `felix-file://` resolver, and now a Postgres-backed ledger — and the fourth brought a
        dependency class the others do not have. The tell is that the ledger was inserted
        *through the middle* of the magic-number subject, which is no longer contiguous. Seam,
        in dependency order with no cycles: `attachments/ledger.py` (the only module that knows
        Postgres exists), `attachments/store.py` (constants, magic, keys, put/read/delete),
        `attachments/refs.py` (`resolve_file_refs`), with `__init__.py` re-exporting the current
        `__all__` verbatim so no import site changes.
      - Landed: **an image is never sent to a model that cannot see it.** Found in a real run: `quick`
        on a deployment defaulting to a text-only Workers AI route answered every picture with "I
        can't see images". The catalog gained `text_only` (vouched, not the `("text",)` default every
        entry inherits), a route can declare `modalities`, and a turn carrying an image goes to
        `spec.model.vision_model` / `FELIX_DEFAULT_VISION_MODEL_ID` or is a 422 naming the route.
        Since: the vision route fails over to the fallbacks that can see, and a fallback that
        answers on either chain is metered as itself.
      - Landed: **images in tool results.** `ToolOutputDict.attachments` carries an image for the
        model to see; Anthropic renders it inside the `tool_result`, the OpenAI wire as one user turn
        after the run of tool messages (that API takes images on user turns only). Stored through
        the attachment store, so the log keeps a reference; screened on a `tool_image` surface when
        `image_model` is set; quarantined with the tool's text. The browser screenshot is the first
        producer, and stops arriving as base64 text. Without `image_model` an untrusted tool's
        images are quarantined (fail closed); 4 per call, 16 per run; a caller's images are kept on
        user turns only. Since: a stored tool image reuses the transcript its inline bytes were
        screened under, so it is read once, not again on replay.
      - Landed: **image tools** (`spec.image_tools`, `image` extra). The model names an image as
        `latest`, `#n` or a stored reference, since it sees pixels and not file ids; results are
        stored and chain.
      - Landed: **A2A FileParts and MCP image content**, both directions. Inbound A2A images are held
        to the upload rules and screened like a turn's; a peer's or a remote MCP server's images are
        untrusted tool images. The vision program is complete: routing (#435), tool-result images
        (#438), image tools (#441), and this.
      - Open, and a change to a security control rather than a feature: uploads are bounded by the
        single global `BodyLimitMiddleware` limit, so a larger ceiling means per-route limits.
        That middleware has a bypass in its history; it should not be widened as a side effect of
        an attachments change. Multipart ingest would also remove the base64 inflation.

- [ ] **Skill import from GitHub** — a skill someone already published should not have to be
      pasted into the editor. Imports land as library drafts through the existing gate; nothing
      resolves a GitHub ref at manifest compile time (`spec.skills` and the loader are untouched),
      so a compile never depends on GitHub being up. Ported from Skillist's mirror sync.
      1. [x] (#471, #476, #477) `felix/skills/{github,importer,sighting_store}.py`, `GET /skill-library/-/browse`,
         `POST /skill-library/-/import`, `felix skills browse|add`, migration `0026` (source
         `import`, origin columns and `lineage_import` on the version, `skill_import_sighting`,
         `skill_policy.import_min_age_days`). `github:owner/repo[/path]` plus a separate ref: a
         commit id (any case) is only a commit, must be what GitHub resolves it to (a hex-named
         branch is `ambiguous_ref`), and is accepted only on the default branch
         (`commit_not_in_repo`); migration `0028` indexes `skill_file(tenant_id, sha256)`;
         `refs/heads|tags/<name>` and bare names resolve through the repository's own refs, a bare
         name that is both a tag and a branch is `ambiguous_ref`; every file read by blob id and
         checked against its git object id, through the egress-pinned client to `api.github.com`
         only. A re-import saves a new version only when the digest of the kept files moved; a
         name whose newest non-rejected version came from another origin, an agent or an operator
         is `origin_mismatch`; the name must be the folder's; an operator upload's name is
         refused. Imports are never published in the import request. An import, every version
         built on it, and an agent's save copying any of its files (`lineage_import`) is held to a
         stricter gate (advisory blocks, only bundle scenarios count, an agent's edit needs a
         person); what `activate_skill`/`read_skill_file`/`list_skills` return of one is screened
         as untrusted tool output where content screening is on (the compile warns where it is
         off), and the system-prompt catalog fences its description (`untrusted="true"`) and
         withholds one carrying injection markers. `FELIX_SKILL_IMPORT_SOURCES` is per tenant
         (`acme=github:acme/*`); with `FELIX_SKILL_IMPORT_GITHUB_TOKEN`, boot refuses (but on a
         development box with auth off) an empty list, an unbound entry and an owner glob. Every
         GitHub call is charged per tenant and deployment-wide
         (`FELIX_SKILL_IMPORT_CALLS_PER_HOUR[_TOTAL]`); a browse lists at most 50. A supply-chain
         cooldown ported from Skillist's install policy (`minReleaseAgeDays`):
         `FELIX_SKILL_IMPORT_MIN_AGE_DAYS`, raised per tenant (tighten-only), counted from Felix's
         own first sighting of the exact files (on any ref, stamped on every browse and import
         attempt, cooldown on or off; pruned after 366 days), never from a commit date the pusher
         sets. Open: a tree GitHub truncates (past ~100k entries) is refused rather than walked;
         a manifest without content screening gets only the free marker floor over imported text
         (warned at compile), which a paraphrase passes; laundering by paraphrase, a partial
         copy, or a changed non-whitespace character (a look-alike letter included) is not
         detected (the copy rule compares text, not meaning); versions saved before migration
         `0032` have no normalized digest and never gain one, so they match by bytes only until a
         backfill from the object store (a worker task or CLI command) exists; the suggester
         still sees an imported skill's name; adopt shares `skills:write` with every library
         write, and an operator can still save imported bytes into a new skill unchecked.
         Fixed since: the skill suggester hands the
         decision model an imported skill's listed description only (withheld on injection markers, quoted as
         third-party text), never its body; a paraphrased description can still bias the ranking,
         which returns only probabilities. `POST /skill-library/{name}/versions/{v}/adopt`
         (`felix skills adopt`, `FelixClient.adopt_skill_version`, migration `0031`
         `skill_version.adopted_from`) clears `lineage_import` going forward: a reasoned operator
         draft of the same bytes, never published by the adopt, audited `skill_adopted`, refused
         for an agent's undecided draft (`agent_draft`); earlier versions keep the mark, an
         agent's copy of adopted text into another skill is still tainted, and a file an edit
         keeps unchanged from a parent without imported text is not a copy, so edits of an
         adopted skill stay clean. The copy rule (`skills/copy_rule.py`) also matches a
         normalized digest (NFKC, format characters removed, casefold, whitespace collapsed; a
         SKILL.md's body only; `skill_file.normalized_sha256`, migration `0032`, no backfill)
         from 32 normalized characters, while any text file with something left after
         normalizing, and any non-empty binary asset, still matches by bytes; an empty,
         whitespace-only or format-characters-only text file matches neither way.
      2. [x] Update checks: `felix/skills/{upstream,upstream_store,bundle_diff}.py`,
         `GET /skill-library/{name}/-/upstream` (the stored ref with `skills:read`, or `?ref=` with
         `skills:write`, re-resolved under the tenant's allowlist; a per-file diff against the live
         version, else the newest, fetching only text files whose git blob ids moved; answers capped
         at 16,384 characters a file and 131,072 in all, and no side over 256 KiB or 4,000 lines
         diffed or fetched; redacted), `POST /skill-library/{name}/-/update` (the importer from the
         stored origin, a draft, no publish field; `skill_draft_saved` with reason `updated from …`),
         `GET /skill-library/-/upstream` (25 a page, a repository and ref resolved once, `stopped` on
         a spent budget or past 30 s, `refresh=false` from the record), 409 `not_imported`,
         `felix skills outdated|diff|update` (diff headers written by the CLI, hunks indented), and
         `upstream` on the library detail. A check stamps the sighting, so asking starts the
         cooldown; a stored default-branch ref resolves as the branch beside a tag of its name.
         `FELIX_SKILL_IMPORT_CHECK_HOURS` (0 = off, ≤168) runs `skill_upstream_checks` on the worker
         (50 skills a tick, its own `skill_job_lease` row, half of each budget, a spent tenant left
         out of the rest of the tick, GitHub's own rate limit ending it) and records each skill's
         upstream state in `skill_upstream` (migration `0029`, backfilled from imported versions);
         `felix doctor` notes that the worker needs the API's allowlist and token. Open: the felix-web
         docs (management API, concepts, deploy settings, persistence); a check of a skill over the
         import caps is refused (`source_too_large`) rather than summarised; a skill checked under a
         `?ref=` is not recorded, so the listing never shows a what-if; the sweep shares the API's
         budget only through Redis, so a worker without it would spend a budget of its own; a spent
         tenant's rows still lead the next tick's read (one refused call, then it is left out).
         Closed: update notifications -- `skills/update_notify.py` queues one signed, metadata-only
         `skill.update_available` per new upstream digest on a recorded check (the sweep, a stored-ref
         check, a refreshed listing; an import records and never queues), and the
         `skill_update_notifications` cron sends it to the endpoints `FELIX_SKILL_UPDATE_WEBHOOKS`
         binds to the tenant (no wildcard; an unbound or unopened id refuses the boot and is dead at
         send time), 50 a tick and 10 per tenant, through `durability.webhooks.WebhookSender` -- the
         completion webhooks' signing, egress, retries and dead letter, counted in
         `felix_webhook_delivery`, now labelled by `kind`. The `webhook-id` is derived from tenant,
         skill, digest and the row's queue generation; a newer digest supersedes an undelivered one,
         a stale check replaces nothing, and an event whose digest the origin moved past or someone
         imported is `superseded`, never sent. `FELIX_WEBHOOK_TIMEOUT_SECONDS` is now at most 60.
         State on the `skill_upstream` row (migration `0030`). Open: the felix-web docs for it.

- [~] **Per-user (personal) skills** — the library was tenant-scoped: `skill`, `skill_version`
      and `skill_file` key on `(tenant_id, name)`, `created_by` / `author` are attribution only,
      and `build_tenant_agent` (`runtime.py`) never sees the caller, so a skill one person or
      their agent saves is the whole org's once published. A personal skill is visible only to
      the principal that owns it, inside one tenant (tenants stay orgs; nothing follows a user
      across tenants).
      - **Owner.** `owner TEXT NOT NULL DEFAULT ''` joins the key of `skill`, `skill_version` and
        `skill_file`; `''` is an org skill, so existing rows needed no rewrite. `skill_feedback`,
        `skill_eval` and `skill_upstream` stay keyed by name alone: feedback, evaluation, import
        and update checks are org-only, and the `~me` routes refuse them. The value is
        `{issuer}|{subject}` (`library_keys.personal_owner`) — a bare subject collides across an
        API key and a JWT. Anonymous callers and `auth_mode=none` have no personal skills. Objects
        go under `skill-library/{tenant}/~{sha256(owner)[:32]}/{name}/…` (`~` cannot start a skill
        name; no raw subject or email in a key), spelled by the store's own `object_key`. Owner
        filtering lives in the store, beside the in-memory twin; tenant RLS is unchanged.
      - **Compile.** The auth middleware turns the verified principal into
        `AuthContext.skill_owner`; `build_tenant_agent(..., skill_owner=)` takes it with no default
        → `BuildDeps` → `load_manifest_skills(owner=)`. Request paths pass the caller's; a durable
        fiber records it at enqueue and the worker's resume compiles with it; eval and scheduled
        jobs pass `None` (org only); sub-agents inherit through the shared deps. Order: host dirs → caller's personal live skills → org library
        → operator uploads, so a personal skill shadows an org one of its name for its owner only;
        an explicit pin to an operator upload still wins.
      - **Opt-in.** `spec.personal_skills: off | read | write` (steps 2 and 3c), default `off` so no stored
        manifest's prompt changes; `read` loads the caller's skills. `write` points
        `create_skill` / `update_skill` at the caller's namespace (3c, after the routes that
        review what it saves) and needs `skill_authoring.enabled`. A validator refuses anything
        but `off` with `skills_declared_only`, which promises an enumerable catalog.
      - **Lifecycle.** The owner publishes their own drafts — no org review queue, no `skills:write`
        — but the security scan, the import copy rule and approvals on the authoring tools all
        still apply: an injected agent persisting a skill into every later session of its user is
        the threat. A per-owner skill cap beside the pending and per-skill version caps.
        `holds_imported_file`'s tenant-wide digest lookup narrows to the owner's and org rows, or it
        answers whether another user holds given bytes. `admin` / `*` may list and archive any
        personal skill; org reviewers do not see them.
      - **API.** `/skill-library/~me/…` mirrors list, get, create, versions, publish, rollback and
        archive for the caller alone. `POST /skill-library/~me/{name}/versions/{v}/promote` copies
        the bytes server-side into an **org draft** (`source="promoted"`) that takes the normal
        review queue — promotion never skips review.
      1. [x] (#517, #519) Migration `0033_skill_owner`, a store bound to one owner
         (`get_skill_library_store(settings, owner=)`, org by default), the twin, and contracts on
         both arms — including one that walks every `SkillLibraryStore` member as a stranger's
         store, so a new query cannot skip the isolation decision. The downgrade refuses while any
         personal row exists, counted under `app.rls_bypass`. Skill saves fail on replicas still
         running pre-`0033` code until a rollout completes (`docs/UPGRADING.md`). Open: make
         `get_skill_library_store`'s `owner` keyword-required in step 3, where personal requests
         first reach the store, so a flow that forgets it cannot write to the org's library; drop
         `owner`'s `''` server default a release after `0033`, once nothing writes without one, so
         a raw insert that omits it fails rather than landing in the org's library; the felix-web
         `internals/persistence.mdx` key change. Found on the way: `0026_skill_import_origin`'s
         downgrade guard counts imported `skill_version` rows with no RLS bypass, so on managed
         Postgres (forced RLS binds the table owner) it reads zero and never refuses. A published
         revision, so not edited; the `deploy-runbook` checklist now says to count with
         `app.rls_bypass` on before rolling back past it.
      2. [x] Compile threading, durable-fiber owner, catalog order, `spec.personal_skills: off |
         read`, e2e (two callers on one manifest, and a durable resume, each shown its own catalog).
         Found on the way: `activate_skill` and `read_skill_file` read a library skill's files by
         name from the tenant's library, which for a personal skill sharing a name and version
         with a tenant one would have served the tenant's files as hers — each catalog skill now
         carries `library_owner`; and skill activation is stored per manifest for every caller,
         so `activate_skill` echoed names another caller activated, personal ones included — its
         answer is now filtered to the caller's catalog. From review: a skill the manifest names
         in `spec.skills` never resolves to a caller's own (only ambient skills are shadowed);
         `create_skill` / `update_skill` / `submit_skill_feedback` refuse a name that is the
         caller's own in the catalog (`personal_skill`) — they write the tenant's library and
         would have edited, or filed feedback on, the tenant's skill of that name; activation audit
         rows carry `library` (`org` or the owner's digest); an API key configured without its
         own `sub` has no personal library; a fiber's recorded owner must match its recorded
         subject. Open: activation is still shared per manifest (one caller's activation marks a
         same-named skill active for the next), as it was between org users before.
         For step 3: lift the `personal_skill` refusal by pointing those tools at the caller's
         library under `personal_skills: write`; run the full publish gate on a personal publish
         and search the caller's own rows in the copy rule (`holds_imported_file` from the org
         store looks at the org's alone); scope idempotency keys by owner rather than
         `principal_sub` (a reply now depends on the caller's library); `read_version_files`,
         `read_version_file` and `newest_buildable_versions` already take a required `owner`.
      3. Split in three, landed in order. Decided 2026-10-09: an owner publishes their own drafts
         through the full publish gate (no org review queue, no `skills:write`); `admin` / `*` may
         list, read and archive any personal skill, every read of another's audited.
         - [x] 3a. The library layer takes a required `owner` everywhere -- `get_skill_library_store`,
           `save_draft`, `publish`, `rollback`, `reject`, `archive_skill`, `evaluate_version`,
           `shadows_operator_upload` -- and the route handlers take it from their request context
           (`LibraryRequest.owner`, the org's until 3b). A personal library refuses imports and
           adopts (`org_only`, 422) and holds `MAX_PERSONAL_SKILLS` skills in use -- live, or
           with a draft waiting; archiving one, or rejecting its drafts, frees its place (100, soft
           by the saves in flight; `personal_library_full`). Evaluations are kept by skill name
           for the tenant's library, so a personal version has none: where the deployment or the
           tenant requires one, a personal publish is refused with that reason (decided
           2026-10-09: a tenant's bar is never lowered) until evaluations are keyed by owner. A
           personal skill splits no operator upload's name; every library audit event carries
           `library`; the skill detail's `upstream` is null for a personal library.
         - [x] 3b. `/skill-library/~me/…` (list, get, files, preview, create, versions, publish,
           rollback, reject, archive) for the caller; admin access to another's library by its
           digest (`library_label`), reads audited; `make contract` (the library section of
           `schemas/openapi.json` roughly doubles). Shape, from review: `library_request(request,
           access)` resolves owner and required scope from a `library` path parameter (absent:
           the tenant's, `skills:*`; `me`: the caller's `personal_owner`; a digest: admin only);
           the shared handlers live on a router mounted at `""` and at `/~{library}`, the latter
           included first so `GET /{name}` does not swallow `~me`; `/-/review`, `/-/policy` and
           adopt stay on an org-only router. A digest names no owner -- `library_label` is one-way
           -- so admin access needs a store method listing a tenant's distinct owners (both arms
           and a conformance case) to resolve one. Shipped as described, with `list_owners` and
           `GET /-/personal` (owner, digest, skill count, `truncated`); reading `~me` needs no
           scope, writing it `skills:personal` (decided 2026-10-09 from security review: a
           principal is a credential, and a shared one is one library for everyone holding it);
           it is refused (403 `no_personal_library`) to a caller with no library; a digest is
           `admin` only, read and archive, every look audited as `personal_library_accessed`, and
           refused under `auth_mode=none`. Storage is bounded per library by 500 names ever and
           `FELIX_SKILL_PERSONAL_MAX_BYTES` (50 MiB) across every version kept, counted by store
           queries (`usage`); the bundle routes' 12 MiB body limit covers `~me`.
           Deferred from review (admin scale, not exposure): resolving a digest past the first
           `MAX_OWNERS_LISTED` (1000) owners -- page `list_owners` or store the label as an
           indexed column; a cursor on `GET /-/personal` beyond `truncated`; making the
           administrator's audit row a precondition of the look (`record_offline_event` fails
           open), written after the handler decides rather than before, with the issuer; a 404 for
           `~`-prefixed names on the tenant-only routes before their scope check (they fail safe
           today).
         - [x] 3c. `personal_skills: write` (needs `skill_authoring.enabled`): `create_skill`
           saves into the caller's library, refused (never redirected) without one or without
           `skills:personal`, checked at call time against the running caller; `update_skill`
           edits the caller's library when it holds the name (live or a fresh draft) unless the
           manifest names it in `spec.skills`, else the tenant's -- every library numbers from
           `0.1.0`, so `parent_version` cannot tell them apart and the narrower one wins. An
           approval grant binds the library the call saves into (`Tool.approval_binding`, hashed
           into the call signature), so a grant for Alice's save authorizes none into Bob's or the
           tenant's. In publish mode a personal skill that would replace a tenant or host skill
           for its owner is held as a draft for them to publish. Feedback stays the tenant's and
           still refuses a personal skill; `auto_eval` skips a personal save. `Idempotency-Key`
           scopes add the caller's skill owner, so one subject at two issuers is two callers.
           Deferred: approval of a personal save by its owner alone (today any `approvals:write`
           holder); a call waiting on an approval re-resolves its library when it runs, so a
           personal skill of the name created during the wait takes the edit (toward the
           caller's own library only); a preview that already knows the call is refused
           (`missing_scope`, `skill_exists`) still opens a pending row.
      4. Promotion, then felix-web docs (library, management API, manifest reference).
         - [x] Promotion. `POST /skill-library/~me/{name}/versions/{version}/promote` (body
           `{reason?}`, `skills:personal`, 201 with the draft) runs `library.promote`: a version that
           went live in the caller's library at least once (`version_conflict` otherwise, so a
           draft or a rejected one is refused) is copied, byte for byte and server-side, into a draft
           of the tenant's skill of the name -- `source="promoted"`, `author` the promoter,
           `promoted_from` the personal version (migration `0038_skill_version_promoted`; the owner
           is never stored on the tenant's row). It follows the tenant's newest version that was not
           rejected (`expect_newest`, so a racing save is `parent_changed`), or starts the skill
           (`MUST_NOT_EXIST`); a name whose every version was rejected takes a promotion again,
           building on nothing. Only the caller's `~me` has the route (a separate `me_router`); the
           tenant's mount 404s and an administrator's digest is a 422. A name whose tenant skill
           carries imported text is refused (`origin_mismatch`) until a reviewer adopts it: the
           promotion would become the newest version an update builds on, freezing the import's
           updates and staling its adopt. Promoted text is judged as an agent's, each rule read
           from one table, `skills/sources.py` (`SOURCES`, a row per `SkillSourceKind`, and a test
           holding the two equal): only a bundle-scenario evaluation counts, the copy rule runs on
           it and it inherits the personal version's `lineage_import`, its `evals/` must equal the
           tenant parent's exactly -- none added, changed or removed (`invalid_bundle`, never
           stripped) -- an undecided one cannot be adopted, and an agent's publish-mode edit of it
           is held for a person, as is an edit of any undecided draft chain that leads back to it
           (`library.builds_on_unreviewed_text`, walked through undecided drafts and bounded by the
           version cap; an unknown source counts as needing review). Review-queue bound: one
           undecided promotion per tenant skill (`promotion_pending`, 409) and
           `MAX_PENDING_PROMOTIONS` (20, the agent pending cap's default -- there is no setting,
           the cap is a manifest field) per promoter (`pending_cap_reached`, 429); both an early
           count (`count_drafts`, which replaced `count_pending` for every draft count, on both
           store arms with conformance cases), soft by the promotions in flight. Audited as
           `skill_promoted` with the personal library's label. The downgrade refuses while a
           promoted row exists, counted under `app.rls_bypass`.
           Known and accepted (security review): a `skills:personal`-only caller learns a little
           about the tenant's library from a promotion's answer -- whether a name exists, is
           imported, or holds a pending promotion, and the version the draft follows. The
           `skill_promoted` audit event links the promoter's subject to their library's digest,
           which an `audit:read` holder can then match to `~{digest}`.
           Deferred: a hard (transactional) promotion cap; telling the promoter when their draft is
           decided; promoting into a name only rejected versions hold has no race check.
         - [ ] felix-web docs (library, management API, manifest reference).

### B. Close the durable loop

- [ ] **Record each tool call's result as it lands** (felix-run/felix#574). #531 logs a batch's calls before they
      run, so a re-run sees them, but their results are still written once the whole batch
      returns. A call that finished inside a batch that did not is therefore closed as
      interrupted ("may have already taken effect"), and the model is asked to check work it
      could have been told was done. Correct, but it costs a turn and an approval each time a
      worker dies mid-batch. Append each result as its call returns, and have
      `_interrupted_tool_results` close only the calls with none.

Not a gap, checked this cycle: the lease is renewed in flight (`fibers.py:443`, renewal loop at
`:473`), so `FIBER_LEASE_MS` bounds "how long after a worker dies is its fiber stranded", not
"how long may a step take". The replay-on-long-approval bug that shape implies was already found
and fixed; the comment at `fibers.py:36-46` is the record.

### C. Operator console

Done — every item is in [HISTORY.md](HISTORY.md) under *Roadmap tidy (Oct 2026)*.

### D. Truth in advertising

Done — the items are in [HISTORY.md](HISTORY.md) under *Roadmap tidy (Oct 2026)*. Kept here
because it is still the first thing an adopter checks:

Checked and *not* a gap, so nobody "fixes" it: `allow_unattended` is enforced — at compile, under
`eu_ai_act` at `risk_tier: high` (`governance.py:209`), which is why `contributor.yaml` carries a
comment explaining exactly that. It is conditional, not inert.

---
## Next (this quarter)

### Harness parity program (gap analysis, 2026-10-10)

Felix against Claude Code, Codex CLI, OpenClaw and Claude Cowork, each Felix cell verified in the
tree. It leads on governance, the skill library, durability, eval and tenancy. It trails on
delegation the model drives, coding-tool depth, and everything that lets an agent live in a user's
world (channels, triggers, self-scheduled work, per-user state, connectors, an interactive browser).
Each wave is shaped by the extensibility rule: what another harness bakes in lands here as a
registry entry, a plugin or a skill, not as a fixed workflow.

- [~] **W0 — silent defaults found by the audit.** The job schedule parser read `*/N * * * *` only,
      and fired anything else — the docs' own `0 3 * * *` included — every 60 s. It is now real
      five-field cron plus macros, refused at write, and a stored unreadable schedule stops firing
      and records why. `limits.precount` (read by nothing) is retired through `compat.RETIRED`.
- [~] **W1 — delegation and planning.** *Landed:* `spec.delegation` binds a `task` tool the
      model calls to hand a job to a child agent — compiled beside `sub_agents` (store first,
      cycles and depth refused, pinned), fresh context, the child's own inbound auth checked per
      call, governed as a tool and by its own stack, untrusted output, held to the parent's caps
      (`LimitState.ceilings`) and `max_peer_hops`. Background children (`delegation.background`):
      a durable run per child on its own thread linked by `parent_session_id`, pinned, on what
      is left of every budget above it, no nested background, `max_background` in flight per
      thread, expiring with what started it, read with `task_result`; `subagent_start`/`subagent_end` frames
      for `task`. *Next:* a `task` entry that names a peer so local and A2A delegates share one
      tool; the same frames for router/parallel/groupchat (*Headless / contract*). Plan mode: see W2. `todo_write` (any pattern, via `spec.tools`): the run's checklist, read off
      the current branch onto the snapshot as `todos` (follows rewind/fork), announced as
      `todo_updated`.
- [~] **W2 — permissions, hooks, commands.** *Landed:* per-thread permission modes (`default |
      accept_edits | plan | bypass`, `spec.permissions`, `POST /chat/mode`) as one deliberate new
      wrapper slot just outside approvals; plan mode runs only `read_only` tools and ends through
      `exit_plan_mode` and the built-in `plan-approval` rule; `bypass` needs `approvals:bypass`,
      checked at set and per run. Declarative `spec.hooks` (session_start, user_prompt_submit,
      pre/post tool, stop, subagent_stop) over signed HTTP to registered endpoints, `on_error`
      per hook. *Next:* sandbox-command hook handlers (needs the shell isolation routing pulled
      out of the shell tool); `/name args` resolved against `spec.prompts`; output styles;
      `spec.network.allow_hosts`, landed with *credentials the model never holds*.
- [ ] **W3 — coding toolset.** `glob`, `multi_edit`/`apply_patch`, persistent shell sessions and
      background processes in the shell runner, file checkpoints restored by `/chat/rewind`, a git
      worktree per child task, and clearing of stale tool results.
- [ ] **W4 — assistant layer.** Inbound triggers (the *Start a run from an inbound event* item,
      pulled forward); a `felix_channels` plugin package (Slack and Telegram, then email) behind
      `felix/plugins.py`; `schedule_task`, heartbeat jobs and a `notify` tool; memory scoped per
      user, plus an agent-editable `USER.md` and `SOUL.md`.
- [ ] **W5 — Cowork layer.** A per-user OAuth vault generalised from GitHub connections, with MCP
      OAuth on top of it; a stateful browser tool; PDF attachments and first-class output artifacts,
      with office-document skills; installable bundles through the skill-import gate; a stock
      approvals preset for destructive actions.
- [ ] **W6 — clients.** A TypeScript SDK generated from the wire contract, and a `felix chat` REPL.

### Harness

- [ ] **Performance audit (2026-10-08): the harness rebuilds per request what the last one had.**
      Read, not measured — each step below carries its own before/after. In landing order:
      - [x] *Model wire.* Every model call opened its own `httpx.AsyncClient`, so each turn,
            judge, screen and decision paid a TCP connect and TLS handshake; they now share a
            per-loop pool (`felix_ai.wire.transport.shared_transport`). The streamed open
            retries 429/529 like the plain POST did, streamed thinking and tool-argument deltas
            join in linear time, and retrieved tools are chosen once per run in manifest order
            (re-ranking per step changed the cache prefix's front). Left as is on purpose: the
            memory prelude after the system prompt — see `_with_prelude` for the trade.
      - [x] *Compile-path caching.* The resolver remembers "not in the store" for the pointer
            TTL (every bundled-manifest request was a DB round trip, ×N on `/v1/models`); parsed
            library SKILL.md by key and digest, 5 min, so a compile skips the GETs; MCP discovery
            per server ref for 60 s, gathered across servers; AWS/GCP secret lookups off the loop
            with one SDK client per process and values for 5 min; the local JWT key parsed once.
            Still per compile, on purpose: memory facts (volatile) and the compiled agent itself.
      - [x] *Session log reads.* `compacting` (every bundled manifest's strategy) reads the log's
            shape -- seq, kind, role and the tree/summary metadata keys -- and full rows only from
            the newest on-branch summary on: 37.9 → 16.4 ms per render on a 2,020-event thread
            against local Postgres, more over a network. 0036 drops the btree that duplicated the
            `session_events` primary key, and a streamed turn asks Redis whether it was aborted at
            most every 250 ms instead of per token. Declined: folding the leaf UPDATE into the
            append transaction -- `store_leaf` is separate so a failed leaf write cannot lose the
            events, and the saving is a few LAN round trips. Still whole-log: `full_replay`,
            `windowed:N`, `semantic:N` and `summarizing` (they have no checkpoint to start from),
            the thread snapshot, and `/chat/sessions/feedback`; the skeleton pass itself is O(n).
      - [x] *Streams and governance fan-out.* A notified stream skips the poll's grace window
            (~13 queries per idle cycle, not ~42); screening verdicts are kept 10 min by window
            text and screener, and a long text's windows are screened 4 at a time; free judges
            run first, model judges together; Presidio runs on one dedicated thread and the fs
            object store's I/O in threads; attachments are read once per request, not once per
            model call. Declined: `max_tokens` on the screener (a reasoning model would answer
            nothing and every screen fail closed), and a process-wide attachment cache (a deleted
            image would outlive its deletion, and base64 images weigh on a 2 GiB VM).
      - [x] *Durable streams stop polling at 10 s.* The status writers turned out to be few:
            `_save_fiber`, its fallback `_record_attempt`, and the claim, each of which now
            announces on the run's thread. Approvals announce when they open and when they are answered, and
            client tool requests when they open and close. Both streams that were pinned to
            `poll_max` -- `durable_run_gen` and a reattach with a run in flight -- relax to the
            long ceiling, and a durable wait never runs past the run's deadline. Pinned per writer
            and per backend (`tests/conformance/test_gates_wake.py`). Not announced: a worker
            that dies mid-step writes nothing, so its run is found by the poll, now within a
            minute rather than ten seconds.
      - [x] *Worker and data hygiene.* Audit and usage flushes insert 1,000 rows a statement: a
            full buffer was one INSERT past Postgres's 65,535 bind parameters, refused, and written
            back one row per transaction. A scheduler tick claims a due job by compare-and-set on
            its `next_run_at` (`jobs.store.claim_run`), so two ticks that read the same due job
            fire it once. Retention deletes 5,000 rows a transaction, by `ctid`.
      - [x] *pgvector recall and the HNSW indexes.* This entry had the diagnosis backwards: the
            indexes *are* used. For a tenant holding most of a table the planner serves recall's
            order from the global HNSW index and sorts the tiebreakers on top, and the scan visits
            `hnsw.ef_search` (40) candidates before the tenant filter runs. Measured on synthetic
            clustered vectors, pgvector 0.8, 100k rows: document recall asked for 40 and got 35.
            The fix this entry prescribed, an index-ordered inner query re-sorted outside, was
            worse: a 5k-row tenant got 1.4 rows of 16. Each vector channel now sets
            `hnsw.iterative_scan = strict_order` for its transaction (`felix.db.vector`), which
            scans until the `LIMIT` is met or `hnsw.max_scan_tuples` (20,000) are visited; the
            queries are unchanged, and a tenant the planner scans exactly is untouched. Still
            approximate for a large tenant (about 93% of the exact top-k in that measurement), by
            choice: exact costs a full scan of the tenant per recall. pgvector before 0.8 runs as
            before and logs once. Per-tenant partial indexes were ruled out: DDL per tenant.
            Document search's two channels now run in a savepoint each, as memory recall's do: a
            failed lexical channel had left the transaction aborted under the vector channel.
      - [ ] *An idle backoff in the fiber loop.* One cheap `SKIP LOCKED` claim a second, against up
            to the backoff in latency before a new durable run starts, unless submit wakes the
            worker. Not taken in the audit's run.
      - [x] *`GET /chat/sessions` pages.* It read every `thread_state` row a tenant had, unordered,
            on each call. It now returns newest first, `limit` (default 100, at most 500) a page,
            with a `felix.cursors` keyset cursor on `(updated_at, thread_id COLLATE "C")` and an
            index that serves that order (`0037`). The sort key is the row's second, not the
            metadata's millisecond `updatedAt` the row reports, because the column is what the
            index orders; the two move in one transaction, so they differ only inside a second,
            where the id breaks the tie. A client that never sends `cursor` now sees its 100 most
            recent threads rather than all of them.

- [ ] **Tamper-evident audit chain** — `seq` + `prev_hash` + keyed HMAC per row, per tenant,
      with `verify_chain` reporting the first break. Allocate the chain at write time inside the
      insert transaction under a per-tenant advisory lock (`_lock_thread` in `session/store.py` is
      the precedent), so a `DurableBuffer` drop does not read as tampering. Hash a `payload_sha256`
      column rather than the payload bytes — `jsonb` does not preserve key order. Retention needs
      a pruning anchor or it breaks the chain it prunes. Pairs with **audit export** in C.
- [ ] **Framework mapping earns its name, or loses it.** `validate_governance` is 55 lines of
      compile-time flag assertions with no mapping to a control id (no CC6.1, no Article 14) and no
      artifact — nothing produces "here is your evidence for control X". `_has_boundary_control` is
      satisfied by `any_limit(manifest.spec.limits)` — the *declared* limits, not the backfilled
      `EffectiveLimits` (corrected 2026-09-29: this said the backfill made it unfalsifiable) — so
      any single declared limit passes it, which is weak rather than empty. Either produce a signed
      compile receipt (`manifests/pin.py` already stores a content hash per thread and is the
      closest thing to evidence in the system), or rename the field so `frameworks: [soc2]` stops
      inviting a reading it cannot support. The schema disclaimer is right and is in the file
      nobody reads.

- [~] **Eval scoring depth** — landed: trajectory rules (`tools_called`, `tools_not_called`,
      `max_tool_calls`, `max_errors`), read off the run's messages and off `mock_tool_calls` /
      `mock_tool_errors` under `--mock`, with `invalid_rubric` for the shapes that cannot reject.
      `fixtures/eval/contributor.json` is the first dataset that scores the *agent*. Still no
      regex, no schema check, no numeric tolerance, no significance test on comparative runs. Nobody can gate a model change on this without writing their own scorer.
      A new rule inherits two things: `_score_answer`'s docstring states the empty-value policy,
      and `tests/unit/test_eval_gate_can_fail.py` reads the rule names off the function, so the
      rule fails there until `negative.json` has an item that has seen it reject something.
- [!] **Long-context price tiers** — deferred on purpose: there is nothing to bundle. Anthropic
      bills Claude 4.6 and later at one rate across the full 1M window, and the pre-4.6 entries
      are sized at 200K. The prescription ("rates per deployment via a manifest price override")
      also could not have worked: `spec.model.price` is `dict[str, float]`, so it cannot carry the
      `tiers` list `_apply_tier` reads, and nothing configurable reaches that code. Revisit only
      for a provider that actually tiers — then widen the override, rather than bundle a guess.
- [ ] **Sandbox ladder extras** — capability-bridge / gVisor as documented extras, not the default
      lean image. The workspace tools need this more than the snippet rung does: see
      [WORKSPACE.md](WORKSPACE.md), which puts workspaces in a hosted sandbox service in production and keeps gVisor for the
      `broker` fallback.
- [ ] **OAuth / dynamic provider keys** — secrets backends cover static keys; refresh /
      `getApiKey(provider)` only if a real customer path needs it.
- [ ] **`append_batch` read-modify-write** — fold the read into the insert
      (`INSERT … SELECT coalesce(max(seq), -1) + :offset … RETURNING seq`) to shorten the lock
      window to one round trip. Deferred as the highest-risk change the ASGI audit named: it is
      the write path for every session event, the multi-row sequence allocation has to move into
      SQL, and the in-memory twin allocates differently. Conformance against real Postgres is
      mandatory, not optional.

- [ ] **Start a run from an inbound event** (proposed 2026-10-06). Runs start from a person
      (`/chat`), a schedule (`/jobs`) or a peer (A2A), but never from something that happened
      elsewhere: a pushed commit, a failed payment, an alert. No route accepts one (`app.py` mounts
      no `/hooks` or `/triggers`), and nothing verifies `x-hub-signature-256` or a Standard Webhooks
      signature on the way in, only on the way out (`durability/webhooks.py`). Seam: a
      `routes/triggers.py` that owns per-endpoint signing secrets (`secret:NAME`, rotated like
      completion-webhook secrets), verifies before parsing, maps the payload to a prompt through a
      template the operator wrote, and hands it to `fire_job` (`jobs/scheduler.py`) or
      `start_durable_chat` (`durability/runs.py`), which already take a `trigger=` label and a
      principal. Hard parts, in order: replay (the `webhook-id` dedupe the outbound side has, read
      the other way), the payload is **untrusted input reaching the model** (screen it as a tool
      result, never as the operator's prompt), and per-endpoint rate limits so a noisy sender
      cannot spend the tenant's budget. Pairs with `spec.execution.webhooks`: event in, result out.
- [ ] **Credentials the model never holds** (proposed 2026-10-06). Today a secret reaches a tool
      as an MCP `Authorization` header, an MCP stdio `env`, or a container's auth, all resolved at
      compile time, and the defence on the way back is exact-string `[REDACTED]` over tool output,
      the session log and audit (`builder.py`, `session/store.py`). Exact match is the gap: a
      stdio server or shell child that holds a key can return it base64'd, split or reversed, and
      that reaches the model. `http_fetch` has no header field at all, so no fetched API can be
      authenticated without handing the key to something the model drives. Proposal: an
      operator-owned map from host (or host + path prefix) to `secret:NAME`, applied inside
      `GuardedAsyncTransport.handle_async_request` (`security/egress.py`) **after** the egress
      check, so the credential is attached only to a request already allowed to that host, and
      the model, the tool arguments and every log see only the request it made. Then an
      `http_fetch` that can call an authenticated API, and MCP HTTP servers moved onto the same
      map. Stdio children keep env secrets and stay the documented exception.
- [ ] **Fail over on an exhausted provider, not only a failing one** (proposed 2026-10-06).
      `spec.model.fallbacks` advances when `_is_provider_error` (`patterns/model_composites.py`)
      says so: `>= 500` or `429`. A quota 429 therefore fails over, but **402** (AI Gateway credits
      empty, see the note under A), a `400` whose body says the credit balance is too low, and a
      connection error with no status all fail the run with a fallback configured and unused.
      Widen the classifier to 402 and the billing markers `_HARD_LIMIT_MARKERS` already lists
      (`felix_ai/wire/transport.py`), never to 401/403, which is a key to fix rather than a
      provider to route around. Separately, and as a decision rather than a fix:
      `limits.max_cost_usd` is a hard stop (`check_budgets` → `trip()`); a manifest may want
      "past this, continue on route X" instead. If it does, that is an explicit `on_budget:`
      field the compile pin sees, never an implicit downgrade.

#### From the governance mutation audit (Sep 2026, #141–#150)

Carried forward intact. These are the exempt kind under the meta-work budget: several are
security findings on the durable-run authority path, and they came from mutating live controls
rather than from re-reading a file. The wave itself is written up in [HISTORY.md](HISTORY.md).

- [ ] **Governance-gap counters vs the tenant metric allowlist.** `runtime.py`'s
      `_apply_metric_allowlist` runs before `build_agent`, and `observability/metrics.py` drops
      any counter not in `spec.observability.metrics`. So a tenant-authored manifest that sets
      that field for any reason silently suppresses `felix_untrusted_tools_unscreened`,
      `felix_policy_unsatisfiable` and `felix_rule_targets_nothing` — the exact signals
      `deploy/GOVERNANCE.md` now tells operators to watch. The WARNING still fires, so this is
      partial. **Decide:** should governance-gap counters be exempt from the manifest allowlist?
- [ ] **Should `felix validate-manifest` hard-fail on a pattern matching no declared
      integration?** Compile-time tolerance exists for the dynamic tool set (a failed MCP
      discovery binds nothing). At author time the builtins plus declared refs are statically
      known, so `github__*` against a builtin-only agent is a typo with no runtime excuse.
      Author-friction call.
- [ ] **felix-web docs lag #148–#150.** `internals/governance.mdx` covers screening and glob
      targeting; the durable-run authority model, the lease semantics and the RLS ordering are
      only in `deploy/GOVERNANCE.md`.
      (2026-09-29: web now covers durable-run authority (`internals/governance.mdx`), the claim
      lease (`guide/deploy.mdx`) and RLS basics (`guide/concepts.mdx`); still only in
      `deploy/GOVERNANCE.md`: #150's lease-versus-approval-timeout semantics and RLS ordering.)

### Headless / contract

- [ ] **Sub-agents on the stream** (proposed 2026-10-06). A composite manifest's children are
      invisible to a client. The router and the delegating patterns pipe a child's frames through
      `_pipe_stream` (`patterns/delegating.py`) unmarked, so a `tool_start` from the child reads
      as the parent's; groupchat stamps `[name]` into transcript *text* only; `parallel` runs its
      children with `invoke()` under `asyncio.gather`, so nothing they do streams at all, only the
      synthesis; and children run with `thread_id=None`, so the session log has no record of them
      either. Proposal: `subagent_start` / `subagent_end` frames (`{agent, path, depth}`, `end`
      carrying the outcome) from `_forward` / `_delegate`, every piped frame tagged with the `path`
      of the agent that produced it, and `parallel` moved onto merged `stream_events` so siblings
      interleave on the wire. Both new events go into `schemas/sse-events.json` and `felix-run/web`
      (`check-protocol-parity` fails until they are modelled), and the tag is an optional field so
      a client that ignores it still renders one flat turn. The session log is the larger half:
      without a child record, a reload shows the flat turn again.

- [ ] **`POST /chat/ui` sub-protocol unspecified** — the route exists and the harness can block on
      a waiter for `DEFAULT_TIMEOUT_SECONDS = 300`. Document the frames and move the timeout to a
      `FELIX_` setting. Related: `request_ui` / `request_confirm` / `request_select` have **zero
      callers in core**, so no tool exposes them and an agent cannot currently ask the user a
      structured question — a capability-surface item hiding in a documentation one.
      (2026-09-29: the frames are specified in web `guide/rest-api.mdx` → UI prompts, so
      "unspecified" is too strong; still open: the 300s timeout is a constant with no `FELIX_`
      setting, and `request_ui`/`request_confirm`/`request_select` have no callers, so no tool
      exposes them.)

### Control plane

- [ ] **A per-tenant spend cap, before `open` signup.** Invite-only signup (2026-10-05) gives
      each invited GitHub account a personal tenant on this deployment's model credentials.
      Nothing caps one tenant's total spend: `limits.max_cost_usd` is per run and fails open for
      an unpriced model. A cap over a window, enforced before a call and failing closed for an
      unpriced one, is what `FELIX_GITHUB_SIGNUP=open` would need.

### Testing strategy

From the audit of 2026-09-05. Phase 1 (the `tests/e2e/` harness), the vendor-credential hole in
`scripts/test.sh` and the invariant that pins it shipped together; the rest are queued in leverage
order. One hardening item per cycle under the meta-work budget; the scanner guards were this
cycle's, and the route contracts below are the next capability-adjacent step.

- [~] **Route contracts through the e2e harness.** The nine `/chat/sessions/*` routes and both
      lease endpoints are covered (`tests/e2e/test_chat_sessions.py`), and the fixture now serves
      one shared script queue so a multi-request flow can be written at all. The run controls
      (steer, follow-up, abort, continue, fork, rewind, thinking, ui) followed, and the spy now
      records the prompts and the model specs. `/chat/compact` is covered on all three paths,
      including the one that actually summarises — which only became reachable once the route
      stopped reading `keep_recent_tokens: 0` as 20000. Still open: the `/chat/continue` success
      path (both tests are its 400 guards), `/chat/rewind` at its default `summarize: true`,
      `/chat/fork` with `from_event_id`, and a steer against a run genuinely in flight. The
      management routers are covered (`tests/e2e/test_mgmt_routes.py`): jobs, eval datasets and
      runs, audit and its metrics rollup, and approvals including a decide, under
      `auth_mode=api_key` so the scope gates are exercised rather than skipped. Two things
      there remain unpinned and are marked as such in the tests: the eval route's `tools`
      hand-off (no dataset item calls a tool) and its `use_llm_judge` inversion.
      `POST /eval/runs/compare` still has no caller at all. `patterns/delegating.py` still has no named test, and needs a per-client
      sub-queue in the fixture before it can have one — the last piece of this item.

- [~] **Postgres arms for the ten stores that have none.** Approvals, session search and the
      fiber *claim* path are done (`tests/conformance/test_approvals_store.py`,
      `test_session_search.py`, `test_fiber_claim.py`) behind a generic `store_settings`
      fixture, and each of them found a real divergence: the approvals twin returned the oldest
      matching grant where Postgres returns the newest, an expired grant could hide a live one
      on Postgres only, and a batch of Temporal-backed fibers starved a tenant's ordinary ones.
      Manifests are done too, and found two more: the twin accepted a canary weight the CHECK
      constraint refuses, and handed back the stored document by reference. Still open, in the
      order their SQL diverges most from the twin: plans and a2a tasks.

      Jobs is done (`test_jobs_store.py`) and found two orderings that were not orders at all:
      `list_runs` broke ties by nothing, so the twin's stable sort returned a job's *oldest*
      runs as its most recent, and `list_jobs` was unordered on both arms.

      Audit is done (`test_audit_store.py`). It found a defect both arms shared rather than a
      divergence: the cursor carried only a millisecond timestamp, so paging stepped over every
      event sharing the boundary millisecond and returned them on no page at all.

      Eval is done (`test_eval_store.py`) and found the worst one yet: `put_dataset` overwrote an
      existing item on the twin and raised `UniqueViolation` on Postgres, so the second run of any
      eval failed on every real deployment while CI stayed green — and the continuous-eval sweep,
      which swallows a per-tenant exception, had scored nothing since its first ever tick while
      reporting a normal result.

- [ ] **Parametrise the cross-tenant sweep arm over every `rls_bypass()`.**
      `test_a_cross_tenant_sweep_still_sees_every_tenant` covers `list_tenants_with_events`
      and nothing else. There are seventeen bypasses (counted 2026-09-29) — `memory/store.py`,
      `durability/fibers.py` (six), `durability/webhooks.py` (two), `audit/store.py` (two),
      `manifests/store.py`, `jobs/store.py`, `jobs/retention.py` (two), `attachments.py` and
      `artifacts.py` — and each can lose its bypass with the whole suite green,
      because only this arm builds a role the policy applies to. The failure mode is not a
      cross-tenant read: the schedulers re-bind per tenant afterwards, so it degrades to a
      sweep that reads an empty tenant list and reports success. That is the shape an operator
      cannot diagnose from outside, and it only bites deployments that opted into
      `FELIX_DATABASE_RLS`. The `rls_settings` fixture already builds the role, so this is a
      parametrisation rather than new machinery. Worth a line in `deploy/GOVERNANCE.md` too,
      beside the RLS guidance, since that is who it happens to.

- [ ] **`test_migrations.py` still wants an autogenerate-empty check** (models versus
      migrations drift) **and stepwise per-revision up/down**; today it only goes base to head
      in one hop.

- [ ] **The per-channel savepoint costs a round trip each, and that scales with latency.**
      Measured: +1.0 to +1.15 ms per recall on loopback, *flat* as the corpus grows from 500 to
      20,500 rows — six extra round trips (`SAVEPOINT` and `RELEASE` per channel) at ~0.19 ms
      each, so it is fixed overhead rather than corpus-dependent. On a managed Postgres at
      ~1 ms RTT that is roughly +6 ms per recall, which is 4–6x the tiebreak cost below and
      lands inline in a turn. It buys the thing the comment always claimed and did not deliver:
      one broken channel loses a channel rather than the turn. If the latency matters, the
      cheaper shape is optimistic — run without savepoints and, on the first failure, roll back
      once and retry only the remaining channels inside them — which costs nothing on the path
      that always succeeds. More code for a path that only opens mid-upgrade, so measure the
      real deployment before taking it.

- [ ] **The recall tiebreak costs an HNSW index scan its selectivity.** Measured at 20,500
      rows: `ORDER BY embedding <=> :vec` alone is an `Index Scan using idx_memvec_hnsw`
      pulling 16 rows in ~0.35 ms, while adding any tiebreak turns it into an
      `Incremental Sort` over that index pulling 391 rows in ~1.25 ms — 4x the time and 5.6x
      the buffers. Going from one extra key to three is then free (~4%, inside run-to-run
      spread), so the lever if this ever matters is dropping the tiebreak, not trimming it.
      It is worth the millisecond today: an undecided cut changes *which* memories a turn
      gets. Re-measure before changing either way, and note the planner does not choose the
      index at all below a few thousand rows, so small deployments pay nothing.

- [ ] **`kind` is unindexed, and recall now filters on it.** `memory_vectors` has no index on
      `kind`, so on the full-text and topic channels `kind = ANY(...)` is a heap recheck after
      the GIN scan — a selective `kinds` over a broad tsquery walks a long way to fill a
      `LIMIT 16`. Unmeasured, and expected to be invisible at realistic sizes; measure before
      adding an index rather than adding one on the strength of this line.

- [ ] **A read is a copy in one store and a window in three.** The jobs store now deepcopies
      its JSON columns on read *and* write, because the twin was handing back the dict it held
      and Postgres deserializes fresh — so a caller editing what it read edited the store, on
      one backend only. `audit/store.py`, `usage/store.py` and `memory/store.py` still return a
      shallow `dict(row)`, which aliases their JSON column the same way. Nothing mutates a read
      today, so this is latent; what makes it worth recording is that the repo now answers the
      same question two ways in files edited by one commit, and the next store copies whichever
      neighbour its author opens. The carrier would be a shared conformance assertion that
      every store's read is mutation-isolated, not four more deepcopies.

### Repo / release hygiene

- [ ] **Repo structure and developer-experience pass** (audit 2026-10-10), one PR at a time:
  - [x] `tests/support/`: the 13 root helpers move into one package. The 18 cross-test imports go
        through it, an invariant forbids new ones, and `factories.py` holds `make_settings`
        and `app_client`.
  - [x] `tests/fixtures/` (`fixtures/skills` moved there), `tests/README.md`, one SSE payload
        parser and shared SKILL.md bodies in `tests/support/`, 48 more helpers on `make_settings`,
        1746 redundant asyncio markers gone, `--strict-markers`/`--strict-config`, unraisable
        exceptions as errors, and the root reset now covers plans, A2A tasks and the ledgers.
  - [ ] Test follow-ups: ~20 `**kw` settings helpers and ~170 inline `Settings(database_url=
        "memory://…")` calls onto `make_settings` (then an invariant against new literals), 89
        hand-built ASGI clients onto `app_client`, 66 `parents[N]` onto `tests/support/paths.py`.
  - [x] Onboarding: `make bootstrap`, `make db` for the without-Compose path, a `make check` that
        skips ty loudly on a lean install (and `check-ci` that fails it), generated `make help`, local
        lock/deps-age/compose/helm checks (Compose via one script CI shares), a README that starts
        with building your own agent, and a README for the plugin example. felix-web's getting
        started page follows in its own PR.
  - [x] Layout:
        - `clients/cli.py` became `felix chat`.
        - The self-build skills moved to `manifests/self/skills/`, inside the boundary and out of
          every install's catalog. `skills/` itself stays as the bundled catalog.
        - The image now copies `skills/`; before, every bundled skill ref was an empty stub in Docker.
        - 111 done items moved to `HISTORY.md`.
        - The `felix_boundary.py` rename was dropped: a `pull_request_target` script loaded by
          module name is not worth touching for a naming nit.
  - [ ] Module splits, one per PR: `manifests/builder.py` (2,270 lines), `patterns/react.py`.

- [~] **Required status checks + `CODEOWNERS`** — the status-check half is **done** and this
      entry was wrong: `main` requires 13 contexts, all bound to the Actions app, and the
      `changes` job does report success so doc-only PRs stay mergeable. Found by hitting it —
      renaming a job for the CodeQL matrix broke `CodeQL` with "not set by the expected GitHub
      app", which only happens when protection is real. Two things follow. **`CODEOWNERS` still
      does not exist**, so review remains convention. And a required context is now coupled to a
      *job name*: rename one and merges block repo-wide with an error that names GitHub apps
      rather than the rename. `security.yml` pins its own name with an aggregate gate job;
      nothing stops the next rename elsewhere, and an invariant comparing required contexts
      against the names the workflows actually produce is ~20 lines
      (`.github/workflows/*.yml` → job `name`, matrix expanded).
- [ ] **Postgres 18** — `pgvector/pgvector:0.8.6-pg18-trixie` exists. Own branch with a rollback
      plan: compatibility pass over the revisions, FTS index, RLS, and a dump/restore path.

### Product (`felix-run/web`)

- [ ] **Session-control UX gaps** — export JSONL from the UI, clearer lease-contention copy,
      reconnect-to-snapshot after a hard refresh, empty/search states.
      (2026-09-29: done — export JSONL (web #66), reconnect-to-snapshot after refresh (web #156,
      the thread in the URL), empty and search states; still open: lease contention falls back to a
      shared lease silently, with no copy saying another tab holds it.)
      (2026-10-04: the harness half is done — that fallback was itself a `409 lease_held`, so the
      second tab held nothing. A `shared` acquire now observes an exclusively held thread with its
      own token and `held_by_other: true`, renews only its own hold, and is refused on the driving
      routes when it sends `X-Felix-Lease-Token`. Still open in chat-ui: the copy, renewing the
      observer hold, and sending the header.)
- [ ] **Prune leftover TS-harness skills/copy** in the docs sync sources. The getting-started
      rewrite landed (that item is done, and this file claimed otherwise until 2026-09-02); the
      residual TS-era prose elsewhere did not go with it. (2026-09-29: a search of both repos'
      docs, skills and READMEs for TS-harness wording found none. Name the files, or close this.)

### Deploy

- [ ] **Scheduled smoke and live eval are red, and have been since 2026-10-04** — `smoke.yml`
      gets `401 invalid_token` from api.felix.run: its `API_KEY` secret no longer matches a key
      production accepts. `eval-live.yml` runs with an empty `FELIX_ANTHROPIC_API_KEY`. Neither
      blocks PR CI, which is why both stayed red through three releases. 0.9.0 was verified by
      hand instead: `felix doctor` and a compile of all twelve bundled manifests in the container.
      Re-mint the smoke key, set the eval key, and confirm one green run of each. Still red
      after 0.12.0 rolled (2026-10-08): that release was checked by `/health` alone, and the
      durable-path fixes in it await a hand-run `cowork` check on make.felix.run.
- [ ] **GKE dogfood** — Helm + ESO → one known-good install note under `deploy/gcp/`.
- [ ] **AWS smoke checklist** — mirror the GCP path (Secrets Manager / S3) in `deploy/aws/`.
- [ ] **Postgres RLS dogfood** — migration `0006` + `FELIX_DATABASE_RLS=true` on a non-prod
      branch; verify retention bypass + mixed-tenant audit flush.
      (2026-09-29: CI now enforces RLS in conformance (`test_rls_enforcement.py`, #204), which
      found and fixed a real usage-flush bug (#341); the dogfood itself — a non-prod deployment
      with `FELIX_DATABASE_RLS=true` — has not happened, and no test covers a mixed-tenant *audit*
      flush through `audit/store.py`.)
- [!] **Rotate Anthropic API key** — only when you say go. Then Secret Manager
      `felix-anthropic-api-key` + recreate API/worker.

---
## Later / explicit non-goals

- [!] **`FELIX_POLICY_BUNDLE_PUBKEY` / OPA** — no signed policy-bundle
      runtime in v1 (governance stays manifest compile + wrappers).
- [!] **Commerce / billing plugin** — seam only; no Stripe in-tree.
- [!] **Cloudflare compute in the harness** — Workers/DOs out of `felix`;
      CF hosts web + named tunnel to GCE API only. Scoped to *compute*: the
      `workers_ai` model provider is an outbound HTTPS call to api.cloudflare.com,
      the same shape as every other hosted provider, and does not reopen this.
- [!] **Merging web into `felix`** — keep repos split.
- [!] **Third-party TUI / CBOR / npm package installer** — session/loop
      ideas only; composition is YAML + `felix.plugins` entry points.
- [!] **Multi-cursor session views ("lanes")** — several named cursors over one
      entry tree, each with its own leaf and queue, as a substitute for
      multi-writer storage. Elegant, but `fork` / `rewind` plus Redis leases
      already cover what Felix does; this would be a session-layer rewrite for a
      problem not yet hit.
- [~] **Snapshot-authoritative streaming** — every command result carries the
      full post-command snapshot with a monotonic `revision`, and progress
      deltas are explicitly advisory and never reduced into authoritative state.
      A better contract than the current SSE union. Not worth doing on its own —
      fold it into **wire-contract snapshot** under Repo / release hygiene when
      that lands.
- [~] **Shared vs exclusive session leases** — leases are binary today. Distinct
      shared/exclusive modes with asymmetric detach-vs-dispose semantics are
      worth remembering when reconnect-to-snapshot UX gets built.

---

## Shipped

Moved to [HISTORY.md](HISTORY.md) — the wave-by-wave record of what shipped and what each wave
taught, including the audit conclusions that did not survive being measured.
