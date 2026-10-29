# Felix roadmap

Living tracker for what to build next. Update status in place; keep items
concrete enough to pick up in a single session.

**Repos:** `felix-run/felix` (harness) · `felix-run/web` (chat-ui + docs)
**Live:** [api.felix.run](https://api.felix.run) · [make.felix.run](https://make.felix.run) · [docs.felix.run](https://docs.felix.run)
**Last reviewed:** 2026-10-23 (cutting 0.18.0: required trigger revision pins (#680/#681), agent steps toward run approval (#683), Claude Haiku 5.5 pricing and the 5.5 default routes (#682); no open item closed); before that 2026-10-23 (cutting 0.17.0: per-person memory (#677), agent workflow authoring and run approval (#671/#676) landed; person-bound threads added as open); before that 2026-10-22 (cutting 0.16.1: the memory audit's duplicate capture, task expiry and embedding backfill landed in #666/#669; per-person memory scope stays open under Harness); before that 2026-10-22 (cutting 0.16.0: self-scheduling, notify, unattended runs and heartbeats landed through #663; the W4 entry is current); before that 2026-10-15 (cutting 0.15.0: the workflows console and #647 marked done, the open workflow decisions listed); before that 2026-10-10 (completed items folded into HISTORY.md); before that 2026-10-08 (after 0.12.0 rolled to production; the full open-item check was
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

- [~] **Google sign-in and a pinned kids agent** — browser redirect sign-in with Google
      (`felix/auth/google.py`, `/auth/google/{authorize,callback,exchange}`) for the accounts
      on `FELIX_GOOGLE_ACCOUNTS` only, keyed by verified email with an optional `sub` pin. An
      entry's `manifests` list is minted as a `manifests` claim that `enforce_inbound_auth`
      holds on every invoke surface, and the bundled `kids` manifest is the agent such an
      account is pinned to (no tools, no memory facts, PII blocked, every reply judged). Left:
      the chat-ui button (felix-web), and a live sign-in with a Family Link account — Google may
      require a parent to allow third-party sign-in for an under-13 account. The reply judge
      falls back to the keyword heuristic when its model call fails, so a judge outage weakens
      `kids` rather than closing it; a fail-closed option for judges is the follow-up.

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

Done — every item is in [HISTORY.md](HISTORY.md) under *Roadmap tidy: completed items folded in (Oct 2026)*.

### D. Truth in advertising

Done — the items are in [HISTORY.md](HISTORY.md) under *Roadmap tidy: completed items folded in (Oct 2026)*. Kept here
because it is still the first thing an adopter checks:

Checked and *not* a gap, so nobody "fixes" it: `allow_unattended` is enforced — at run time, by
the approvals wrapper, which refuses a gated call in an unattended run (a scheduled job and what
it starts) unless the rule sets it; and at compile, under `eu_ai_act` at `risk_tier: high`
(`governance.py`), which requires it to be `false`.

---

## Next (this quarter)

### Harness parity program (gap analysis, 2026-10-10)

Felix against Claude Code, Codex CLI, OpenClaw and Claude Cowork, each Felix cell verified in the
tree. It leads on governance, the skill library, durability, eval and tenancy. It trails on
delegation the model drives, coding-tool depth, and everything that lets an agent live in a user's
world (channels, triggers, self-scheduled work, per-user state, connectors, an interactive browser).
Each wave is shaped by the extensibility rule: what another harness bakes in lands here as a
registry entry, a plugin or a skill, not as a fixed workflow.

- [x] **W0 — silent defaults found by the audit.** The job schedule parser read `*/N * * * *` only,
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
- [x] **W2 — permissions, hooks, commands.** *Landed:* per-thread permission modes (`default |
      accept_edits | plan | bypass`, `spec.permissions`, `POST /chat/mode`) as one deliberate new
      wrapper slot just outside approvals; plan mode runs only `read_only` tools and ends through
      `exit_plan_mode` and the built-in `plan-approval` rule; `bypass` needs `approvals:bypass`,
      checked at set and per run. Declarative `spec.hooks` (session_start, user_prompt_submit,
      pre/post tool, stop, subagent_stop) over signed HTTP to registered endpoints, `on_error`
      per hook. Command handlers were built and dropped before merge: the only places Felix can
      exec are inside the agent's writable workspace, so the agent could rewrite or hijack its own
      hook; a local hook is a `private` sidecar endpoint instead (`deploy/GOVERNANCE.md`). Slash
      commands: a user turn `/name args` naming a declared `spec.prompts` entry is replaced by its
      expansion on `/chat` and `/v1` (any other slash is sent as written), and `GET /v1/models`
      lists them as `felix.commands`. Output styles: `spec.output_styles` (inline prose, a `default`),
      chosen per thread by `POST /chat/style`, appended to the top-level agent's system prompt;
      listed on `/v1/models`, shown on the snapshot. Per-agent egress: `spec.network.allow_hosts`
      (host, `*.` subdomain or IP patterns), enforced by `http_fetch` per hop in the guarded
      transport (the browser is refused under a list: interception cannot confine it), checked against the manifest's own
      URLs at write; children compile under their parents' lists, background children carry them
      on the durable run. Credentials the model never holds: `FELIX_EGRESS_CREDENTIALS` (operator,
      per tenant, one exact host + port + path prefix) named by `spec.network.credentials`, the
      header attached by the transport after the host check, https only, masked if echoed.
      *Follow-ups:* MCP HTTP servers onto the same map; the browser behind a local proxy enforcing
      the bound -- every socket, workers' included -- which lifts the refusal of `browser_tools`
      under a list.
- [x] **W3 — coding toolset.** *Landed:* `glob` (the backend's `tree` walk filtered with
      `full_match`, files only) and `multi_edit` (ordered exact-string edits, all or none: read,
      apply in memory with `edit_file`'s rules, `write_file_checked` against the read's digest),
      both composed from existing backend operations so the hosted gateway needs nothing new.
      `apply_patch` was folded into `multi_edit`: a unified diff is a second grammar for the same
      change, and exact strings are what models already produce for `edit_file`. `glob` matches a
      segment at a time with `fnmatch` (atomic per `*`; `PurePath.full_match`'s one regex ran for
      minutes on `*a*a*a*a*a*a*b`), bounded wildcards, off the loop on a deadline. Hosted writes
      and edits now share the checked write's per-path lock. *Follow-ups:* the compare in the
      gateway helper's `write`, through its replace-by-rename, so a hosted checked write is one
      atomic call (it is three today, and truncates in place); a `tree` rooted at `glob`'s `path`
      (hosted walks the whole scope, one gateway `list` per folder). Clearing stale tool results
      (`spec.session.clear_tool_results: {keep_turns, min_chars}`): each render stubs a tool result
      behind the newest `keep_turns`–`2·keep_turns − 1` user messages, over `min_chars` or carrying
      an image, keeping the call paired and a spilled result's artifact id (parsed from the tail in
      the exact shape `artifacts.spill_marker` writes, so a tool cannot forge one and a run of
      `[artifact:` cannot stall the loop); the log is untouched, what a caller sent is never
      rewritten, and clearing runs inside replay image screening (`model_view_strategy`, the one
      composition both render sites call). *Follow-ups:* the compacting strategy still sizes the
      window on the full results, so it can compact before clearing would have made room; a router
      passes its cleared strategy to its children, so a child is cleared by the router's setting
      too. Background commands (`shell_tools[].background`): `run_in_background` starts a command
      under a detached supervisor (`tools/shell_background.py`, stdlib only, ported into the
      gateway helper) and answers an id; `shell_output` reads what it wrote since the last read and
      `shell_kill` ends its group. All three exec places — development box, shell runner (`/start`,
      `/output`, `/kill`), hosted sandbox (`exec_start`/`exec_output`/`exec_kill`) — run the same
      code, and the hosted and local backends answer a session identically. No shell interpreter:
      a persistent `bash` session was declined, since it would void the argv allowlist.
      *Follow-ups:* the hosted supervisor's survival past the helper's exit, and tini as the
      Container's PID 1, are pinned only against the fake gateway, not a deployed Container; a
      background command ends when its hosted sandbox idles out; caps are per root and per host,
      not per tenant. File checkpoints (`tools/workspace_checkpoints.py`, ledger in `tools/checkpoint_ledger.py`, on
      by default, `FELIX_FILE_CHECKPOINTS`): every write, edit, multi-edit, delete and rename the
      model's file tools make in a thread's own workspace records the file before and after,
      anchored to the assistant event whose call made it; `/chat/rewind` with `restore_files` puts
      back each file changed on the abandoned branch, refuses one that moved on since (`conflicts`),
      says what it could not keep (`skipped`) and when it ran out (`truncated`), and is audited
      (`workspace_restore`). Every rewind then forgets the branch it left, so a second rewind to the
      same point never reaches past the first. Content kept once per tenant by digest under a byte
      ceiling; ledger `file_checkpoints` (migration `0039`, RLS, memory twin, conformance and RLS
      arms) swept a file's whole history at once, counted from its last change. Shared scopes (`tenant`, `deployment`) are not recorded.
      *Follow-ups:* a shell command's writes are not recorded; rewinding onto another branch undoes
      the abandoned one but does not replay the target's; deleting a thread does not purge its
      checkpoints before retention; a truncated restore forgets the captures past its cut.
      A git worktree per child task (`repos/worktrees.py`): a background child of
      a thread with a local repository always works in its own worktree on `felix/task/<id>`, a
      foreground one under `spec.delegation.worktree` (bound to its own thread's workspace while it
      keeps the parent's request). A foreground child's parent is told the branch, its commits and
      whether work was left uncommitted, and an untouched worktree is removed with its branch; a
      background child's start message names its branch. A repository whose configuration names a
      filter driver gets none, and a thread holds at most eight that hold work; an untouched
      background child's goes when its run ends. *Follow-ups:* telling the parent what a
      background child left in its worktree when it finishes; a route or tool for the parent to
      release a child's worktree before the TTL (a child thread id is not addressable by
      `DELETE /{thread}/workspace/repo`); `open_file` from a child shows the parent's checkout,
      since `/chat/workspace/file` serves the conversation thread's; the clone's own `_git_run`
      (`describe`, `list_files`) lacks the worktree git's fences (ceiling, protocol, filter check);
      a repository whose own branch is named `felix` or `felix/task` cannot take a `felix/task/<id>`
      branch; an invariant holding workspace readers to `workspace_thread(ctx)` rather than
      `ctx.thread_id`; typed clone and worktree states behind `read_checkout`, and the git runner
      and its prelude moved out of `github_publish` into `repos/`; a hosted
      equivalent (clone the parent's branch into the child's sandbox); merging a child's branch is
      the parent's own git. W3 is complete with this; *Next:* W4.
- [~] **W4 — assistant layer.** *Landed:* inbound triggers (`POST /triggers/{id}`, the *Start a
      run from an inbound event* item below); the `felix-channels` plugin package with Slack
      (`packages/channels`: a mention or DM starts a durable run as `slack:<team>:<user>`, a Slack
      thread is a Felix thread, and the answer goes back through the new core
      `register_run_finished` plugin hook). *Follow-ups:* approvals and `ask_user` as Slack
      buttons; Telegram, then email, as further adapters; per-user account linking; streaming
      partial answers; at-least-once delivery for run-finished hooks (row-backed and swept like
      `webhook_state`), so a Slack outage does not lose a reply; one core `start_run_as(...)` for
      the start-a-run-as-a-principal flow triggers and channels each copy; the worker running
      plugin startup hooks (channel secrets masked there before the first reply); a `felix doctor`
      warning for `FELIX_CHANNELS` set with no plugin installed. *Landed:* agent
      self-scheduling (#651): `spec.scheduling` binds `schedule_task`, `list_scheduled` and
      `cancel_scheduled`, which write ordinary jobs on the manifest as the caller
      (`jobs/agent_tools.py`) -- `jobs:write` required, a per-caller cap, a minimum recurring
      interval, a one-shot `at:<ISO-8601>` schedule in the jobs grammar, and a default approval rule
      on `schedule_task` that no permission mode waives (`permission_mode.UNWAIVABLE_RULE_IDS`). A
      refused call is denied in the approval preview, before any row is written; a credential shared
      by every holder (a sub-less API key's `api_key`) owns no tasks; a task records its writer's
      token `exp` and is refused and disabled once past it (a one-shot set after it is refused at
      write); scheduled work -- a firing and anything it starts, a workflow's agent steps included
      (`AuthContext.scheduled`, carried in the recorded caller) -- cannot schedule more; and fired
      one-shot tasks are swept after `FELIX_SCHEDULED_TASK_RETENTION_DAYS`. *Follow-ups:*
      `cancel_scheduled` runs without an approval (a cancel only removes work, but a model can
      cancel a person's task unasked); the per-caller cap is read then written, so two concurrent
      `schedule_task` calls can both pass it; a task's report back into the thread that scheduled it
      (`spec.scheduling.report_back`, *landed*; one kept out by a live run now waits on the thread
      and lands when the run ends, a firing that errors reports that it did not finish, and only a
      task's newest report stays in context, `metadata.context_key`, though every report stays in
      the conversation's log and transcript); a report waiting on a thread whose live
      run died with its process waits for the thread's next run to end (dropped after a day), is
      not re-checked against `report_back` or the job on delivery, and a thread deleted between
      the firing's check and its queueing write comes back holding only the report (the push
      inserts a missing row); a durable or workflow run a task
      starts just before its writer's token expires is not clamped to that expiry, so it may run
      on past it by up to the run's own TTL. *Landed:* `notify`, PR 1 of 3:
      `spec.notify` binds a tool that sends a title and short message as a Web Push to the person
      the run acts for and to no one else (`push/agent_tool.py`, `push/notify.py:send_to_person`,
      `push/store.py:list_for_principal`) -- refused for no named caller or a shared subject; known
      secrets redacted, links stripped and the text screened before it leaves; a per-person,
      per-manifest `max_per_hour` and a `max_per_run`; approval off by default and never waivable
      when on; and from a task's firing it points at the thread the task was scheduled from (only
      for a task the person wrote, and only the tenant's own thread id). With it, the machine
      actors moved to `auth/actors.py` and the person check to `tools/refusal.py`, shared with
      `schedule_task`. *Follow-up:* the per-run count is per request context, so a durable run's
      resume starts it again; carrying it in the fiber state would make it per run. *Landed
      (#656):* `notify` PR 2: the notice is also left in the chat thread it links to, when that is
      not the run's own, as an entry for the person that no model prompt carries (`custom`,
      `metadata.type: notice`) on the active branch, so it is there when the person opens it --
      even when no browser took the push. Not into a thread that does not exist, nor while a run
      is live in it (react now marks itself before its first append, or a durable run); an
      exclusive lease alone does not count, since chat clients hold one while a thread is open.
      The origin is now trusted: `PUT /jobs` ignores a caller's `scheduled_by` and keeps the
      stored one only when the task's own writer re-saves it, and a link or entry needs a chat
      thread (no `:` in the suffix). The result says what happened in the thread
      (`thread`) and lists only the channels that delivered. *Follow-up:* chat-ui draws the entry
      as a generic note ("Note · assistant · not sent to the model"); a notice-specific rendering
      is a felix-web change. Without Redis the run mark is per process. *Follow-up:* the session index's `updatedAt` moves only on metadata writes, never on an
      append, so a thread a notice lands in does not rise in `GET /chat/sessions` -- nor does one a
      turn lands in; the live-run mark (`workspace_notes.mark_run_active`) is set only by the `react`
      loop, so a third-party pattern that writes a thread without running it leaves none. *Next
      for notify:* PR 3 makes delivery a channel seam (`register_notifier`, push the first
      channel, Slack and email to follow). *Landed (this PR):* scheduled and triggered work stops
      waiting on nobody. A job's firing, an inbound trigger's run, and everything either starts
      (workflow steps, durable and background children) is unattended -- `RequestContext.unattended`,
      derived from `AuthContext.scheduled` / `AuthContext.trigger_run`, which the recorded caller
      carries -- and there an approval rule without
      `allow_unattended` refuses the call at once (no row, push, frame or wait), as do a
      command-screening approval and a client tool; `allow_unattended: true` keeps the wait. Found
      in a real run: a gated job parked the serial sweep for the rule's ttl, every tenant's jobs
      behind it. And a running job is not fired again: `claim_run` reads `last_status = running`
      (stale after 6 h, so a crashed worker cannot wedge a job), `fire_job` clears the mark on
      every exit, only over its own claim (`finish_run` compare-and-sets `last_run_at`), disabling
      a job clears a stuck mark, and `POST /jobs/{name}/run` answers `409 job_running` while one is
      in flight.
      *Follow-ups:* the sweep still awaits each firing in turn, so one long run delays the jobs
      after it (fan out per job, bounded); `accept_edits` on a job's thread still waives its edit-tool
      approvals unattended (only `bypass` is re-checked); a job run record does not list the calls it refused
      (they are `policy_deny` audit rows). *Landed:* heartbeat core (#661). The operator
      defines `spec.heartbeat` (checklist, recurring schedule held to `min_interval_minutes` >= 15,
      a firing `model` route, `max_turns`, `keep_threads`; needs `spec.notify`), and a person turns
      their own on or off over `/heartbeats` (`heartbeats:read` / `heartbeats:write`, a named
      non-shared caller, the manifest's inbound auth checked at enrolment as the firing will run
      it). Enrolling writes an ordinary job as the person (`hb-<manifest>-<sha256(person)[:12]>`;
      an API key or Felix sign-in is re-checked at each firing and never expires, any other
      credential's `exp` is recorded and ends it) and their heartbeat conversation
      (`{tenant}:hb-<hex>`, where `notify` leaves each notice). A firing builds its prompt from the
      manifest's current checklist, never the job payload, runs on the heartbeat's model and step
      bound, records `quiet` (no notice sent) and the answer cut to 500, and prunes its firing
      threads to `keep_threads` (`session/prune.py`, both store arms); a manifest that drops its
      heartbeat turns it off. *Landed (this PR):* heartbeat limits. `spec.heartbeat.max_firings_per_day`
      (optional; unset caps nothing) holds a person's firings per UTC day, scheduled and manual
      alike; past it a firing calls no model and is recorded `skipped` (a new run status, not
      counted toward the cap), and the heartbeat stays on; a cap below the schedule's daily firings
      is a write warning. A cancelled or scheduler-failed firing is now recorded before its mark
      clears, so it counts. `FELIX_JOB_RUNS_KEEP` (200) trims every job's `job_runs` to its newest N
      after each run, both arms, never a non-skipped run of the current UTC day (the cap's count).
      *Next for heartbeats:* the chat-ui toggle and a heartbeat-aware rendering of the conversation
      (felix-web), including a muted `skipped` run. *Landed (feat/memory-person-scope):* memory
      scoped per person — `spec.memory.scope: person` (the default), `FELIX_PRINCIPAL_ALIASES`,
      `felix memory assign` (see Harness). *Next:* an agent-editable `USER.md` and `SOUL.md`.
- [ ] **W5 — Cowork layer.** A per-user OAuth vault generalised from GitHub connections, with MCP
      OAuth on top of it; a stateful browser tool; PDF attachments and first-class output artifacts,
      with office-document skills; installable bundles through the skill-import gate; a stock
      approvals preset for destructive actions.
- [ ] **W6 — clients.** A TypeScript SDK generated from the wire contract, and a `felix chat` REPL.

### Authored workflows (2026-10-12)

A workflow is a DAG of typed steps the operator draws in chat-ui (React Flow; the UX brief is
"one canvas, two modes": edit and run on the same graph), run durably and watched live. Steps are
an open registry, not a fixed vocabulary, so this stays inside the extensibility rule.

- [x] **Definition and store** (#634). `felix/workflows/` — `WorkflowSpec`, the step-kind
      registry (`agent_turn`, `tool_call`, `approval`, `branch`), `check()` returning ports and
      problems, and the `workflows` table (`0040`) with revision-checked conditional saves.
- [x] **Routes** (#636). `/workflows` CRUD with ports and problems, revision as `ETag`,
      `If-Match` / `If-None-Match: *`, `/-/kinds`, `/-/check`, `workflows:read` / `workflows:write`.
- [x] **Runs** (#639). Orchestrator fiber, agent steps as child durable chats as the starter,
      approvals, branches, pins checked at start, caps, stopping by id, fail fast.
- [~] **Tool steps and the rest of runs.** *Landed in this PR:* `tool_call` executes — a child
      fiber (`workflow_tool`, op `tool`) calls the tool from its step's `manifest` through the
      governed tool set, after checking the tool is still bound and its live schema still takes
      what the step sends; replay only for `replay_safe` tools. `tool_call.manifest` is now
      required (an older stored step reads as `invalid_config`).
      *Next:* run and step SSE frames (polling until then); four-eyes approval steps; a run
      scope; whether `workflows:read` sees step outputs.
- [x] **Run hygiene.** Finished runs swept after `FELIX_WORKFLOW_RUN_RETENTION_DAYS` (30); the
      recorded starter and pins cleared when a run ends; a step's output capped at 128 KiB and a
      run's record (inputs, each step's inputs and outputs) at 4 MiB; errors cut short.
- [x] **Editor tool listing.** `GET /workflows/-/tools?manifest=` lists the tools a `tool_call`
      step can name — the manifest compiled as the caller (`runtime.governed_tools`, shared with
      the MCP server and the step), each with its input schema and the step's input ports; client
      tools are left out and refused at run. Tool inputs keep the tool's own argument names.
      *Found on the way, since closed:* MCP `tools/call` executed the governed executor directly,
      so the plugin and manifest tool hooks and the `tool_call` audit row did not apply to it. It
      now dispatches through `ToolRunner` too (`for_client`), as the tool step does.
- [x] **Entry points.** Manual run with inputs (#639). *Landed:* a job naming a workflow
      (`workflow_id`, `0043_job_workflow`) starts a run on each firing with `payload.inputs`, as
      `cron` for the job's writer, who needs `workflows:write`; the run's trigger is `job:<name>`.
      The job is held to the workflow revision its writer saw (re-save after an edit). Open: a
      JWT writer's job keeps its grant with no expiry, and each firing lends it for 24 hours.
      A trigger naming a workflow maps payload fields to typed inputs by JSON Pointer; the
      sender's text is recorded as `untrusted_inputs` and fenced as data in every agent prompt of
      the run. Open: per-value taint instead of per-run fencing. Every workflow trigger pins a
      `revision` (required; boot fails without it), re-pinned by hand after each save.
- [x] **Console.** felix-web's Workflows page: the list, the graph with a definition and a run
      view, the editor (web#519–#525), each step's transcript read under its run (#645) and the run
      watched over `GET /workflows/{id}/runs/{run_id}/stream` (#646). The instrument's compact graph
      is dropped: a step's thread is not a chat thread, by design (#645), so it had nowhere to sit.
- [x] **MCP `tools/call` through the runner** (#647): the plugin and manifest tool hooks and the
      `tool_call` audit row now see an MCP client's call, as they see a model's and a tool step's.
- [ ] **Decisions for the maintainer.** Raised while building the above; nothing is done about any
      of them until one is made.
      - **A trigger's `revision` pin.** A trigger naming a workflow runs its current revision unless
        the operator pins one in config, so an edit changes what an inbound sender can start.
        Require the pin, or keep it optional? *Decided (2026-10-23):* required for every workflow
        trigger; a config without one fails the boot.
      - **An outside IdP's job grant.** A job written with a GitHub, Google or Actions sign-in is
        now re-checked at each firing against the account entry or org grant that admitted its
        writer (the sign-in audit, 2026-10-17). One written with a token another issuer signed still
        keeps that writer's grant with no expiry, and each firing lends it for 24 hours. Bound it by
        the token's own `exp`, as a durable chat run's life is, or by a fixed ceiling?
      - **Runs that ended before #644.** Those still hold their starter's recorded authority and the
        manifest pins; #644 clears them only at a run's end from then on. Backfill-clear them in a
        migration?
      - **Two people on one gate.** `FELIX_APPROVALS_REQUIRE_SECOND_PERSON` already refuses a
        decision by the run's own starter; nothing lets an approval step require two decisions.
        Wanted?
      - **What `workflows:read` sees.** It reads every step's inputs and outputs and, since #645,
        each step's transcript, which can carry a trigger's payload. Keep that, or gate outputs and
        transcripts behind a stronger scope?
- [~] **Agent-authored workflows** (`spec.workflow_authoring`). An agent drafts, checks, saves and
      runs workflows for the person it acts for, each save held for that person's approval.
      *Landed:* each workflow records `created_by` and `authored_via` (`0045_workflow_owner`), set at
      creation and kept on every replace, and shown on `/workflows` (#667). The tools
      (`workflow_catalog`, `check_workflow`, `save_workflow`, `list_my_workflows`, `run_workflow`),
      the spec block and its unwaivable approval rule. The owner is the issuer-qualified
      `skill_owner`, checked under the store's row lock. A person's save through the routes adopts
      the workflow: their own keeps it runnable by the agent (`owner:agent:<manifest>`), anyone
      else's takes it away entirely. The tools refuse unattended runs and workflow threads, and mint `wf-<hex>` ids.
      *Open:* the console marking agent-written workflows (felix-web); a Slack or other channel
      caller with no `skill_owner` cannot author.
      *Done:* `run_approval` (`auto` | `always` | `never`, default `auto`): `run_workflow` waits for
      the person when a supplied input can reach a `tool_call` step whose tool is not read-only,
      any agent step, or a step of a kind other than `approval` and `branch`, through `Tool.approval_condition` on a harness-marked conditional rule.
      Waivable by `bypass`, and `auto` stays the default (both decided 2026-10-23).
      *Done:* every workflow trigger must pin a `revision`, so an agent's save never changes what
      a trigger starts (this replaced #680's refusal of unpinned triggers on agent-held workflows).
      *Done:* the unwaivable built-in approval (`schedule_task`, `notify`, `save_workflow`) is one
      table, `permission_mode.BUILTIN_APPROVALS`. The rule ids, both unwaivable sets and the rules
      the builder adds are derived from it, and `test_builtin_approvals.py` compiles every row.
- [ ] Open: whether a workflow can call another; per-step retry and timeout. (A tool schema that
      drifted since a `tool_call` step was wired fails the step at run, #640.)

### MCP on the official SDK, and Skills over MCP (2026-10-22)

An audit of the MCP Skills extension (SEP-2640, `io.modelcontextprotocol/skills`, Final) found it
exists only on protocol revision `2026-07-28`, while `/mcp` was hand-rolled JSON-RPC on `2025-06-18`
with the agent chosen by a non-standard `params.manifest`. Decision: adopt the official `mcp` SDK in
both directions, then serve the skill library over MCP. Loading remote skills live into an unattended
agent is out: the extension's safety model is a person approving each skill's digest set, which maps
onto the library's import gate instead.

- [~] **Server on the SDK.** `/mcp` and `/mcp/{manifest}` are `StreamableHTTPSessionManager`
      (stateless, JSON responses) over a lowlevel `Server`; both eras, `server/discover`, header
      validation and status codes are the SDK's. `mcp` became a core dependency.
- [ ] **Refuse a request carrying `Origin`, app-wide.** `2026-07-28` says an MCP server MUST validate
      `Origin`; Felix has no browser surface and no CORS layer, so a request with any `Origin` can be
      refused (403) in the middleware stack. The gap it closes is DNS rebinding against a loopback
      `make dev` (`auth_mode=none`), and it is not MCP's alone: `/chat` and `/v1` are equally reachable.
      The SDK's own `TransportSecuritySettings` also imposes a Host allowlist, so a plain Felix
      middleware is the better fit. Found in the SDK-server security review.
- [ ] **SDK legacy-path parse errors echo the client's input.** On a request without a modern version
      header the SDK answers a malformed body with pydantic's text (`Validation error: …`), which
      quotes the client's own `input_value`. No server data, but not `client_safe_message` either;
      accepted for now as SDK behaviour, revisit if the SDK changes or a pre-validation is cheap.
- [~] **Outbound client on the SDK.** `mcp/client.py` opens an SDK `Client` per discovery and per
      call, either era (a legacy server's calls skip the `server/discover` probe). The SDK's HTTP is
      httpx2, so the egress guard gained an httpx2 twin (`security/egress_httpx2.py`) built on the
      same `connect_pinned` / `guard_request`; stdio is the SDK's `stdio_client` behind the allowlist
      and the scrubbed env, and now speaks the spec's newline-delimited JSON rather than LSP-style
      `Content-Length` frames.
- [ ] **Follow-ups from the SDK-client reviews.**
      - [x] *Exceptions skipped screening and masking.* `apply_secret_masking` now redacts a
        known secret from any tool's raised text, and `apply_content_screening` screens a covered
        tool's raise as its output, quarantining or blocking a flagged one; both re-raise with the
        inferred error code kept (`tests/unit/test_tool_error_governance.py`).
      - [ ] *The PII guardrail and judges do not read a raised exception.* `apply_guardrails` acts
        on returned output only, so `duplicate key (email)=(alice@x.com)` reaches the model past
        `guardrails: pii` with `block_on_match`. The same catch-and-re-raise shape as masking.
      - [x] *A stdio command's `PATH` was the manifest's.* An allowed name is now resolved on the
        API's own `PATH` and spawned by absolute path (`stdio_policy.resolve_stdio_command`), and
        `PATH` joined the env keys a manifest may not set, so an interpreter started through
        `/usr/bin/env` comes from the operator's directories too.
      - [x] *The stdio env denylist missed variables that run code with no file on the host.*
        Named keys and four prefixes (`LD_`, `DYLD_`, `BASH_FUNC_`, `GIT_CONFIG_`), compared
        case-insensitively, refused at spawn and on write but not at compile; the child's git
        disarmed as the shell tool's (`stdio_policy.GIT_DISARMED`, now with `protocol.ext.allow`,
        `core.askPass` and `GIT_TERMINAL_PROMPT=0`). Left open, in `deploy/GOVERNANCE.md`: keys
        that point at a file, repository-named git drivers, TLS and proxy keys.
      - [ ] *Decide: an env-key allowlist per stdio command.* A denylist is the known keys, not all
        of them, and a server's own settings (`*_API_URL` pointed at an attacker collects the
        `secret:` value sent with it) are beyond any denylist. An operator-defined set of keys a
        manifest may set per allowlisted command closes the class; it needs a settings shape and
        a story for stored manifests. Also open: whether a manifest may override `HOME`, and a
        `felix doctor` check that git is 2.31 or later.
        Was:
        `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n=core.fsmonitor`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`,
        `BASH_FUNC_<name>%%`, `SHELLOPTS`+`PS4`, `JAVA_TOOL_OPTIONS`/`JDK_JAVA_OPTIONS` reach a
        fixed allowlisted binary (an `mcp-server-git`) through `mcp_servers[].env`. A prefix ban
        breaks legitimate keys (`GIT_AUTHOR_NAME`); the fix is a designed allowlist of env keys, or
        `shell.py:_git_disarmed` applied plus a targeted denylist. Needs the stored-manifest
        story: refusing at spawn drops that server's tools, refusing at write blocks the save.
      - *stdio reads are unbounded until a newline* (the SDK buffers a line in full); the old
        reader trusted `Content-Length`, so not new.
      - *Per-call cost.* A call is a fresh session: a `server/discover` probe for a modern server,
        the listing that keeps the SDK from listing after the call, the call, the close. Passing the
        discovered `DiscoverResult` as `prior_discover` with a version pin would drop the probe.
- [ ] **Skills extension on the server.** `skills/list`, `skills/get` and skill files over
      `resources/read` from the manifest's own catalog (one helper shared with `build_agent`),
      manifests of sha256 and size computed from the redacted bytes served. Blocked on an SDK release
      containing python-sdk#3485 (merged 2026-10-11, after v2.3.0).
- [ ] **MCP servers as a skill-import source.** `mcp:` sources into library drafts, each file
      verified against the entry's digest, size and frontmatter, lineage keyed on the digest set.

### Agent Skills runtime against agentskills.io (2026-10-28)

An audit of the catalog runtime (discovery, catalog, activation, resources) against the
agentskills.io specification and its client-integration guide. The library, import and publish
side was already conformant. Four PRs, landed in order:

- [x] **Runtime spec conformance** (#705):
  - Discovery takes only `<dir>/SKILL.md`. Root `*.md` files and a SKILL.md nested in another
    skill are no longer skills. The walk skips hidden dirs and `node_modules`, is bounded, and
    logs shadowing.
  - Cosmetic problems warn and load.
  - A `disable-model-invocation` skill or an unresolved ref is hidden from the skill tools, not
    only from the prompt.
  - The catalog block is sorted, and the `name` argument enumerates the invocable skills.
  - The activation body is capped, `license`, `compatibility` and `allowed-tools` are relayed as
    information, and symlinks are listed consistently.
  - A BOM no longer fails strict validation.
  - New `felix skills validate <dir>`.
- [x] **Per-thread activation** (`fix/skill-activation-per-thread`):
  - "Active" is read from the thread's session log (`skills/activation.py`). Activation used to
    be one row per (tenant, manifest), so `list_skills` said `active: true` in a thread whose
    model never saw the body.
  - The `skill_activation` table is dropped (migration `0047`), and `/skills/{manifest}` no
    longer reports `active`.
  - Activating the same instructions twice in a thread answers `already_active`.
  - `activate_skill` results are never cleared or spilled, and are re-injected when compaction
    or a window drops their turn.
  - `create_skill` / `update_skill` say when the skill is usable: `next_turn`,
    `after_publish`, or `never` under `skills_declared_only`.
  - Retention re-shows only what the compile's catalog still offers, fenced, labelled when
    imported, ahead of the history (not transient), within 128K characters a render. A queue
    write-back under a skill tool's name is stored as `queue:<name>`.
  - Open: whether a write-back may write a `tool_result` under *any* harness tool's name (the
    skill tools are now refused; the general case is a harness-only provenance marker on real
    results). A side question does not get retention, since it renders without a catalog. Two
    `activate_skill` calls in one parallel batch both return the body (both read the log
    before the batch is appended). Retained text is not counted inside a compacting
    strategy's budget; the 128K cap bounds it instead.
- [x] **`/skill-name <params>`** (`feat/skill-slash-invocation`):
  - The harness runs the governed `activate_skill` before the first model step. The body arrives
    as a screened tool result, never as user-turn text, and a declared prompt of the same name
    wins.
  - `SkillRef.preload` activates a declared skill once per conversation.
  - `user-invocable`, `argument-hint`, `$ARGUMENTS` / `$N` are honoured; a
    `disable-model-invocation` skill is still the person's to run.
  - Only a turn a chat route marks as the person's runs `/name`; a person's privileges (a
    model-hidden skill, `arguments`) go by harness-minted call id, once.
  - Open: `/v1/models` `commands` lists prompts only; skills with their `argument_hint` are on
    `GET /skills/{manifest}`. `/v1` counts as a person's surface though its caller may be an
    agent, and A2A/MCP callers do not: decide whether `disable-model-invocation` needs a scope
    of its own. The harness's synthetic assistant turn carries no provenance marker in the log.
  - [x] `/name` in every pattern (`fix/skill-runtime-gaps`): a composite carries the person's
    `/name` turn into each child step (`InvokeInput.person_command`), so every `plan_execute`
    subtask, `parallel` specialist and `groupchat` speaker starts with the skill activated. A
    router already forwarded the turn, and `reflect` reads it back from the thread. Open: a
    router, `parallel` or `groupchat` still cannot run its own `preload` (no tool loop; its
    compile warns), and a `plan_execute` planner sees `/name args` as written, without the
    instructions. A child compiled once and reached twice under `parallel` (two specialists
    naming it) shares one `PersonCalls`: one run's round closing voids the other's id, so that
    `/name` runs as the model's (fails closed; preload had the same race). Fixed with it: the
    route's "already screened" mark names the manifest it screened for. As a bare flag, a
    composite with no inbound controls left it to its first child (a router's pick, one
    `parallel` specialist), which then skipped its own screening. And a person's `/name` now
    runs in a manifest only for a caller its own door admits: a router's pick or a composite's
    child no longer hands a narrower-scoped parent's callers its declared manual-only skills
    (routers had that gap since `/name` shipped). Open: the rest of a child's turn is still
    not held to its door -- a composite whose child needs scopes the parent does not is only
    refused at compile when the parent is anonymous (#714).
- [x] **Forked skills** (`feat/skill-fork`; `context: fork`, `agent:`, `model:`): the skill runs in a
      delegation child as a call of the governed `task` tool, and only its answer returns. Open:
      the tool runner's per-call layer (manifest and plugin tool hooks, the `tool_call` audit row)
      runs for `activate_skill`, not for the fork's inner `task` call -- decide whether a fork
      should go through the runner; a fork costs two `max_tool_calls`; the approval prompt for a
      fork shows the skill body before the model's task (no `approval_preview` yet).
  - [x] General-purpose fork (`feat/skill-fork-self`): a manifest with no `spec.delegation` runs a
        forked skill that names no `agent:` in itself -- compiled lazily on the first fork, through
        a governed `task` withheld from the model; the subagent's own forks run nowhere, and its
        compile reports no warnings or counters the manifest's own compile already did. Open: it
        compiles the manifest a second time per request that forks (MCP discovery included), and
        loads skills, prompt files and MCP tool lists at that point rather than at admission; the
        subagent's catalog still lists the forked skill it cannot run (one wasted turn).
- [x] **`run_skill_script`** (`feat/skill-scripts`): `spec.skill_scripts` runs a script from an
      active, trusted skill's `scripts/`. It runs where a shell tool would, under an interpreter the
      manifest maps by extension and `FELIX_SKILL_SCRIPT_INTERPRETERS` allows. The bundle is
      written per call to `.felix-skills/<skill>/<token>/` (refused to the file tools, removed
      after), and the script runs from the workspace root. The runner admits `kind: skill_script`
      by its own interpreter list, not the shell allowlist. Open:
      - Scripts run from host skills only unless `FELIX_SKILL_SCRIPT_SOURCES` adds `library` or
        `personal`, since an author's grant becomes code on the shared runner. A per-skill or
        per-scope grant would be finer.
      - The hosted workspace backend's sandbox is refused at write (no gateway write path for the
        bundle yet). An object-store skill cannot run scripts, because the store has no listing.
      - The whole bundle is written every call, up to 8 MiB. A cancellation during the write can
        leave a token directory, hidden and gitignored.
      - A script's egress is the runner's network, with no SSRF guard.
      - A library skill's binary assets are not written, since the library returns text.
      - A forked skill is never active, so its scripts cannot run.
      - A concurrent shell tool can rewrite the bundle between the write and the exec, the
        same boundary shell tools already have.
- Decided: `allowed-tools` (experimental in the spec) is surfaced and never enforced or used to
  skip approvals. A third party's SKILL.md must not loosen governance.

### Manifests, connectors and new agents (2026-10-28)

An audit of the bundled manifests, then tools for the things a daily-driver agent is asked
about first. Plan: weather, maps, market data, a per-person connection vault (W5), Google and
Apple connectors, then example agents built on them. One PR at a time, in order:

- [x] **Manifest audit fixes** (#714, review follow-up #715):
  - An anonymous composite naming a child that refuses anonymous callers is refused at compile.
    The bundled `router` was the anonymous door to `deep`'s unconfined fetch.
  - `triage` refuses anonymous callers.
  - Every anonymous bundled manifest has run limits, as calls, time and input tokens. These are
    tokens rather than `max_cost_usd`, because a declared cost cap refuses to compile on an
    unpriced route.
  - Stale fields, prompts and tool names were corrected.
- [ ] **`packages/connectors`**: weather (Open-Meteo, keyless), maps (Nominatim/OSRM), market
      data (keyed).
  - [x] Weather, and the core seam it needs: `Tool.egress_hosts`, checked against the agent's
    `network.allow_hosts` bound at compile. `connectors__weather_current`, `_forecast` and
    `_alerts` (NWS); `FELIX_WEATHER_BACKEND` is an open registry.
  - [ ] Maps, with weather by place name once geocoding exists.
  - [ ] Market data and FX.
  - Found, not fixed: a plugin tool that declares no `egress_hosts` is still unbounded under
    `allow_hosts`. Refusing every undeclared non-`local` tool under a bound is the allowlist
    answer, and would break any plugin tool bound today under one.
- [ ] **Connection vault**: `github_connections` generalised per provider (W5), plus MCP
      `auth: person`.
- [ ] **Google and Apple connectors**: Calendar, Gmail drafts, Drive; iCloud CalDAV/CardDAV.
- [ ] **New bundled agents**: `concierge`, `travel` (`plan_execute`), `markets`, `inbox`,
      `researcher` (`reflect`), `automator`.
- Found, not fixed:
  - **No per-manifest session retention.** `governance.retention_days` shortens only the audit
    TTL, so `kids` keeps children's transcripts for as long as `FELIX_SESSION_RETENTION_DAYS`
    says (forever by default).
  - **`cowork`'s `local_open` opens any URL ungated.** Gating only the URL form needs the
    client contract to carry the URL as its own argument for `when_args`.
  - **A child's `schemes` and `required_scopes` are not checked through a router for an
    authenticated caller.** The anonymous case is refused at compile, reading both doors with
    `admits_anonymous`; a caller holding a key but not a child's scope still reaches it. Closing it
    means the router's door is the union of its children's, decided at compile. Nor is the
    refusal checked at `PUT /manifests`, so a misconfigured stored router answers 500 per request
    instead of 422 at save.
  - **A manifest's `metadata.name` is never compared with the name it was found under** — the
    loader and the object-store resolver take either. The compile now refuses a child whose names
    differ; a top-level manifest that names itself differently is still served.
  - **`felix bundle-manifests --strict` misses inert rules.** An approval rule naming tools the
    manifest never binds is only a compile-time `felix_rule_targets_nothing` warning.

### Auth audit (2026-10-12)

A read of authentication, per-route authorization and tenancy against v0.14.1, landed as four PRs.
Threads stay shared inside a tenant by decision; the PRs close the places where that sharing let
one member act with another's authority.

- [x] **C1, manifest secret refs** (#633). `FELIX_MANIFEST_SECRETS` binds each `secret:NAME` to
      the tenants that may name it; Felix's own, infrastructure and registry-bound secrets are
      always refused.
- [x] **In-tenant escalations** (#635). Opener-only GitHub publish and checkout routes; jobs on
      behalf of their writer through inbound auth (`0042_job_creator`); approvals decided once;
      loosening `/chat/mode` needs `approvals:write`; opt-in second-person approval.
- [x] **Authz gaps** (#637). Off-turn admission on the pane, `/chat/style` and A2A task methods;
      `metrics:read`; empty `FELIX_WORKSPACE_DEPLOYMENT_TENANTS` default; pinned Google isolation.
- [~] **Hardening** (#638). Mount validation, one API-key parser, `mint-jwt` bounds, trigger TTL.
- [!] **Per-principal thread ownership.** A `threads:*-all` scope and an owner on each thread, so
      one member cannot read or drive another's. Deferred: it changes the sharing model and the
      Slack shared-thread case with it. Until then a `felix_manifests` pin is not a confidentiality
      boundary inside a tenant for API keys or JWTs (`deploy/GOVERNANCE.md`).
- [ ] **Token revocation.** Felix-minted JWTs carry no `jti` and nothing can revoke one before
      `exp` (8h for login tokens); Google sign-in has no logout. A deny-list keyed on `jti`, and
      `kid` on minted tokens for key rotation.
- [ ] **A job's standing grant.** A job written by a JWT principal keeps that writer's scopes until
      it is re-saved; API-key writers are re-checked per firing (#635). Decide expiry or
      re-validation for the rest. Likewise a durable run keeps a removed key's scopes until its TTL.
- [ ] **RLS bypass as a role.** `app.rls_bypass` is a session setting the app role can set itself,
      and `FELIX_DATABASE_RLS` defaults off; cross-tenant sweeps should use a `BYPASSRLS` role.
- [ ] **API-key lifecycle.** Keys are plaintext config with no expiry, last-used or rotation.
- [ ] **Device-flow phishing.** `/auth/github/device` lets anyone start a flow and redeem it; a
      deployment that only needs browser sign-in should be able to turn it off.
- [ ] **Trigger dedupe in Postgres.** GitHub signs no timestamp, so replay protection is the
      dedupe record, which is per-replica without Redis and lost on restart.
- [ ] **Checkout edits by non-openers.** Another member driving a checkout thread can have the
      agent edit and commit there; the opener then publishes it. Show the opener the diff's
      authorship before publish, or refuse non-opener turns on a checkout thread.
- [ ] **`ManifestNotFound`.** Each surface rediscovers which exceptions mean "no such manifest"
      (`LookupError` vs `ValueError`); `resolve_tenant_manifest` should raise one type. Four wrappers
      exist today: `runtime.UnknownManifest`, `workspace_files.UnknownManifest`,
      `side_question.UnknownManifestError`, MCP's `_UnknownManifest`; define it in `manifests/resolver.py`.

### Harness

- [x] **Memory is per agent, not per person (prod audit, 2026-10-22).** *Landed
      (feat/memory-person-scope):* `memory_vectors.principal` (migration `0046`, both arms, RLS
      unchanged — person isolation is the store's filter); `spec.memory.scope: person | agent`,
      `person` by default: a person (`issuer|subject`, carried through durable runs and jobs) has a
      pool shared by every agent acting for them, read beside the operator-written rows of the
      agent's own pool; a request naming no person writes nothing; person-pool ids hash the person
      rather than the manifest so two agents storing one fact converge on one row. GitHub and
      Google sign-ins link through `FELIX_PRINCIPAL_ALIASES` (one hop, memory only). Existing
      agent-written rows reach no person until `felix memory assign` moves them. Not carried: a
      Slack channel message names no verified person, so it writes no memory under `scope: person`.
      Still open: whether an agent's newer value may retire an agent's older one on the same topic
      (`memory_store._may_retire_by_topic`), which the injection guard refuses today.
- [ ] **Threads belong to the tenant, not a person.** Person memory isolation stops at thread
      access: a member who posts into another member's thread runs with their own pool, but their
      facts land in that thread and capture learns from its history (security re-review of #677,
      `deploy/GOVERNANCE.md` § Memory: whose memory an agent reads). Person-bound threads —
      an owner on `thread_state`, checked on continue — would close it.

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

- [ ] **Performance audit (2026-10-21): the cost is round trips, not CPU.** Measured, then read.
      A plain `quick` turn through `create_application()` with the scripted model costs 2.6 ms
      (`/chat`) and 4.8 ms (`/chat/stream`) of Python on `memory://`, compile included; the rest
      is I/O. Against Postgres the same turn opened 13 and 15 transactions -- each a pool ping,
      `BEGIN`, the RLS `set_config` and `COMMIT` around its queries, ~70 round trips before the
      model on a managed database. Redis was 4 commands a turn. In landing order:
      - [x] *Chat round trips.* 13 / 15 → 10 transactions a turn, pinned by
            `tests/conformance/test_turn_round_trips.py` (budget 10): the `thread_state` row is
            read once per chat request (`memoize_thread_meta`; every write in the module forgets
            it), a live stream's `id:` cursor comes from the head the turn's appends recorded
            (`store.known_head`) instead of a `head()` per structural frame -- four a tool call --
            and the compacting render's two reads share one transaction (`Session.reading`).
            `tenant_session` no longer sets the RLS tenant twice. Declined: binding the RLS bypass
            once per connection rather than per transaction. A libpq startup option is refused by
            PgBouncer (the statement-timeout note at `db_connect_timeout_seconds`), and a session `SET`
            lands on whichever server connection a transaction pooler picked -- under FORCE'd RLS
            a missing bypass empties every query. Still separate on purpose: `store_leaf`, so a
            failed leaf write cannot lose the events (the 2026-10-08 entry).
      - [ ] *`model_change` on every turn.* `_persist_model_change` appends an event and writes
            the row whenever the request names a model, and `felix_client` names one on every
            request after `set_model` -- three transactions and one log event per turn. Skipping
            it when the stored `model_id` matches is wrong after a rewind past the change: the
            snapshot reads the model from the newest `model_change` *on the branch*. Compare
            against the branch's own, which the render already walks.
      - [ ] *Workflow orchestrator polling.* `POLL_MS = 2000` for a run's whole life, and every
            look rewrites the fiber row (`fibers.py`, op `workflow`) and reads each step in flight
            in a transaction of its own -- ~15-25 round trips a look, ~40k looks for an approval
            waiting a day. Back off when a look changed nothing (2 s doubling to 60 s), wake the
            orchestrator when a child fiber ends or an approval is answered, read steps in one
            `= ANY(:ids)` query, fold `_release_fiber` into the last save. The claim scan filters
            `lease_until`/`wake_at` from the heap behind every leased or sleeping fiber: partial
            indexes on `wake_at WHERE sleeping` and the runnable arm. `count_active` reads a
            tenant's whole run history per start: a partial index on the live statuses. Pairs
            with the fiber loop's idle backoff above.
      - [ ] *Outbound clients.* `safe_async_client` builds a transport -- an `SSLContext` on the
            loop and a TLS handshake -- for every MCP call, `http_fetch`, search, A2A peer and hook
            webhook; the hosted workspace gateway does the same per operation (`gateway_client`),
            times checkpoint reads and up to 400 sequential `tree()` calls. One module `SSLContext`
            first (no risk), then guarded transports pooled per loop by `(allow_http, egress,
            credentials)` -- the pin check runs per request, so reuse keeps the guard -- and one
            gateway client per URL. `manifest_hooks._post` re-parses `FELIX_WEBHOOK_ENDPOINTS` and
            rebuilds secrets per fire.
      - [ ] *The loop and memory on a 2 GiB VM.* Background-shell waits (up to 20 s) run in the
            default executor, six threads on 2 vCPUs, which fs reads and `getaddrinfo` share: an
            async wait loop or an executor of their own. Maps that only grow: `steer._queues`
            (`peek_steer_count` and `request_abort` create one), `tree._leaf_by_thread` /
            `_epoch_by_thread`, the per-path write locks in `workspace_local` and
            `workspace_hosted`. Uncapped Chromium launches; 32 background supervisors at ~20 MB.
            `orphan_worktrees` and a failed clone's `rmtree` block the loop.
      - [ ] *Checkpoint ledger.* `kept_bytes` aggregates a tenant's 7-day history on nearly every
            write (a write's "before" is the last one's unstored "after"): cache it ~30 s -- the
            ceiling is documented soft -- and index `(tenant_id, before_sha) WHERE
            before_state='stored'`. `expired_paths` groups the whole table per batch: drive it from
            the age index with an anti-join. `glob` walks the whole scope whatever `path` says,
            and can spend its 20k entries before reaching it.
      - [ ] *Smaller.* Each token is two frames (`text_delta` and `session_progress`) carrying its
            text four times -- a wire-contract change. Pipeline the run's start and end Redis
            calls; `LPOP key count` for workspace notes; `list_runs` and the run stream read
            `definition_json` and `steps_json` they drop; scheduled manifest jobs run one after
            another inside the cron tick; `read_artifact` downloads the whole object per window;
            context-file discovery is uncached (up to six object-store misses a compile).

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

- [x] **Start a run from an inbound event** (proposed 2026-10-06; landed with W4). `POST
      /triggers/{id}` (`felix/triggers.py`, `routes/triggers.py`): operator-registered in
      `FELIX_TRIGGERS`, Standard Webhooks and GitHub signatures verified before the body is read
      (an open scheme registry for more), replays caught by a signed key in the idempotency store,
      a per-trigger limit counted after verification, a durable run as `trigger:<id>` through the
      manifest's inbound auth and pin. The payload never enters the instruction: the model reads it
      through the `trigger_event` tool as untrusted output, after a marker scan at intake.
      *Follow-ups:* a Postgres-backed replay store (without Redis the claim is per process, and a
      GitHub replay is refused only for the claim's TTL, since GitHub signs no timestamp);
      Stripe and Slack schemes; a trigger whose result goes back to the sender's own API.
- [~] **Credentials the model never holds** (proposed 2026-10-06). *Landed for `http_fetch`:*
      `FELIX_EGRESS_CREDENTIALS` maps an id to one exact host, port and path prefix and a
      `secret:NAME`; `spec.network.credentials` names the ids an agent may send (its
      `allow_hosts` must name each host); `GuardedAsyncTransport` attaches the header after the
      host check, per redirect hop, https only, and registers the value for masking
      (`security/egress_credentials.py`). *Next:* MCP HTTP servers onto the same map, so their
      `Authorization` is no longer resolved into the compiled tool. Stdio children keep env
      secrets and stay the documented exception; exact-string masking of what they return is the
      gap that remains there.
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

#### From drafting the toolkit skills (2026-10-10, #579)

Writing `tools-runtime`, `durable-execution` and `skill-library` meant reading those subsystems
end to end, and these turned up. Each was re-checked against `main` on 2026-10-11 (the cron
fallback one had already been fixed by `parse_schedule`). They came from reading, not from a run,
so under the meta-work budget only the first is exempt as a security defect; the rest are queued.

- [ ] **Browser `path_prefix` holds the first navigation only (security).** The navigation is
      compared as an origin and a normalised path (`security.host_allowlist.within_prefix`, shared
      with `http_fetch`), but redirects and subresources get the SSRF check, not the prefix. Apply
      it to redirects of the main frame in the route guard, and plant the case in a test.
- [ ] **Sandbox `path_prefix` confines nothing.** `tools/sandboxes.py` checks `path` against the
      prefix with a raw `startswith`, and `path` is then never passed to the container. Either
      pass it and check it properly, or drop the field from the ref.
- [ ] **Plugin cron tasks may never be scheduled.** `_register_plugin_cron_tasks` runs only in
      the worker's `WORKER_STARTUP` hook; `felix_worker/main.py:scheduler_main` imports the tasks
      module but never fires that hook, so its `LabelScheduleSource` may never see a `plugin_*`
      task. Not confirmed against Taskiq; the two tests call the helper directly and cannot tell.
      Prove it with a test that builds the scheduler's schedule list, then fix.
- [ ] **A failed run's exception text reaches callers.** `durability/fibers.py` stores
      `str(exc)` as the run's error, returned by `GET /chat/runs/{token}`, the durable stream's
      `run_error` frame and webhook payloads; `jobs/scheduler.py` stores it in job runs, which
      `routes/jobs.py` returns. The route-error-text scan misses stored state. Classify where it
      is written (`client_safe_message`), per the invariant.
- [ ] **Two answers to "is this in-memory?".** `session/store.py` (and session search and
      `thread_state`) treat any URL containing `:memory:` or `sqlite` as in-memory;
      `db/session.py:_use_memory`, behind the memory and plans stores, matches `memory://` only.
      A `sqlite` URL would split the stores. One predicate, used everywhere.
- [ ] **`plugin.json` egress is advisory.** `skills/plugin.py` validates
      `network.allowed_hosts` carefully, and its only reader turns it into a low-severity scan
      note. **Decide:** enforce it for the skill's tools, or say in the docs that it is a
      declaration, not a control.
- [ ] **The hosted workspace backend runs on Workers.** `deploy/cloudflare/workspace-gateway` is a
      Worker → Durable Object → Container chain that `FELIX_WORKSPACE_BACKEND=hosted` depends on,
      against CLAUDE.md's "no Workers / Durable Objects compute" and the non-goal below. It is
      opt-in and reached over HTTPS. **Decide:** amend the rule to allow an opt-in out-of-process
      sandbox provider, or put another provider behind the same backend Protocol.
- [ ] **PR quality gate misses tool files with containment checks.** `.claude/hooks/pr-quality-gate.sh`
      does not match `artifacts.py`, `attachments.py`, `tool_images.py`, `image_tools.py`,
      `document_search.py`, `queues.py` or `client_requests.py`. Fold into the toolkit staleness PR.
- [ ] **Cleanup, one pass.** Stale text: the `tools.py` docstring in `skills/` (the authoring tools
      are in `authoring.py`), the `_UNTRUSTED_TRANSPORTS` comment in `builder.py`,
      `_streaming.py:_has_lapsed` ("no top-level handler"), and `tests/support/optional_deps.py`
      naming the removed `test_temporal_backend.py`. Candidates for the **dead-code-audit** skill:
      `Settings.webhook_secret` (hydrated, apparently unread — run webhooks sign per endpoint),
      `SummarizingSessionStrategy` (exported, never built: `summarizing:N` builds the compacting
      strategy), `EchoExecutor`, and `client_bridge.prepare_waiter`. (The `mcp` extra is gone: the SDK is a core dependency
      since the MCP SDK program below.)

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
      *Part done (2026-10-28):* a foreground `task` child's calls stream as `subagent_tool`
      frames (`{agent, tool_call_id, tool_name, phase, args | result}`) from an outermost tool
      wrapper while `tools/subagent_trace.py` names the child -- live only, not in the log, and
      not from a durable run. The composite patterns' children are still unmarked.

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
