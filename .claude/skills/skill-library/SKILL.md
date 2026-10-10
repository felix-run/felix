---
name: skill-library
description: How Felix loads, stores, imports, screens, publishes and improves Agent Skills — the catalog loader and its precedence rule, the tenant and personal skill libraries, GitHub import into drafts (allowlist, first-seen cooldown, upstream checks), the publish gate and policy, agent authoring, promotion and adopt, and the feedback/evaluation/improvement loop run by the worker. Covers felix/skills/, the `felix skills` CLI and the /skills and /skill-library routes. Use when editing anything under packages/harness/src/felix/skills/, a skill-library route, `spec.skills` / `skill_authoring` / `personal_skills` / `skill_suggestion`, or when a skill does not appear in a catalog, a publish or import is refused, or an imported skill's text is quarantined.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*) Bash(uv run felix:*)
metadata:
  covers: felix/skills/, felix_cli/skills.py
---

# The skill library

A skill body reaches the model through `activate_skill` as *instructions*, not as fenced reference.
Every rule in `packages/harness/src/felix/skills/` follows from that: nothing an agent or a third
party wrote enters a catalog until a person, or a gate no setting can open, lets it.

## Module map

| Stage | Modules | What they own |
|---|---|---|
| Load | `loader.py`, `types.py`, `store.py`, `tools.py`, `suggest.py` | Host dirs + library + operator uploads → `SkillCatalog`; the four skill tools; per-manifest activation rows; the decider hint |
| Format | `format.py`, `binary.py`, `plugin.py`, `semver.py` | SKILL.md parse/validate and bundle path allowlist; base64 assets; `plugin.json`; version bumps |
| Library | `library.py`, `library_store.py`, `library_keys.py`, `sources.py`, `copy_rule.py`, `authoring.py` | Draft/publish/rollback/reject/archive/adopt/promote; rows; owner and object-key spelling; per-source rules; imported-text lineage; agent tools |
| Import | `github.py`, `importer.py`, `sighting_store.py`, `upstream.py`, `upstream_store.py`, `bundle_diff.py`, `update_notify.py` | Pinned GitHub fetch and allowlist; import as draft; cooldown clock; origin checks, diffs, updates; `skill.update_available` webhooks |
| Screen / publish | `format.py`, `review.py`, `security.py`, `publish_gate.py`, `policy.py` | Validation, 0-100 quality score, heuristic scan, the verdict, the tighten-only policy |
| Evolve | `feedback.py`, `feedback_store.py`, `evaluate.py`, `eval_store.py`, `improve.py`, `quality_store.py`, `jobs.py`, `job_limits.py`, `model_calls.py` | Feedback a person decides; baseline-vs-skill evals; rewrite-to-draft; the worker sweep, its lease and per-tenant caps |

## From skill to activation

1. **Bundled or operator skills.** `skills/<name>/SKILL.md` in the repo and `FELIX_SKILLS_DIR` are
   the host catalog (`skills/loader.py:host_catalog`); operator uploads live at the object-store
   keys `skills/loader.py:operator_skill_keys` and `pinned_operator_skill_keys` spell.
2. **Library save.** Every write lands in `skills/library.py:save_draft`: validate
   (`format.py:validate_skill_bundle`), refuse a host skill's name (`host_owns`), review + scan
   (`publish_gate.py:assess`), reserve the version row, then write bytes. Callers: the operator
   routes, `skills/authoring.py:make_skill_authoring_tools` (`create_skill` / `update_skill`),
   `skills/importer.py:import_skill`, `skills/improve.py:run_claimed_improvement`, `adopt`, `promote`.
3. **Publish.** `skills/library.py:publish` / `rollback` → `_make_live` → `_gate`, which re-reads
   the bytes and re-runs `skills/library.py:evaluate_version` (the same verdict the preview route
   shows) under `skills/policy.py:load_publish_policy`. Only then does `live_version` move.
4. **Compile.** `manifests/builder.py:build_agent` calls
   `skills/loader.py:load_manifest_skills` when the manifest declares skills, enables authoring or
   `personal_skills`, or lists a skill tool. Precedence lives in one place,
   `skills/loader.py:_resolve_ref`: catalog, then host, then the library's live version — except
   an explicit `version` pin that an operator upload holds wins over the library; with none of those, the operator's uploads (pinned, then unversioned). `spec.skills_declared_only`
   narrows which names load, never where a body comes from.
5. **Prompt and tools.** `skills/loader.py:skill_catalog_xml` is appended to the system prompt;
   `skills/tools.py:make_skill_tools` binds `list_skills`, `activate_skill`, `deactivate_skill`,
   `read_skill_file` (`SKILL_TOOL_NAMES`). Activation is stored per (tenant, manifest) by
   `skills/store.py:get_skill_activation_store`.

Manifest fields (`manifests/schema.py`): `skills` (`SkillRef`: name, description, version),
`skills_declared_only`, `personal_skills` (`off` / `read` / `write`), `skill_authoring`
(`SkillAuthoringSpec`: enabled, mode `draft`/`publish`, max_pending, auto_eval) and
`skill_suggestion` (`SkillSuggestionSpec`, needs `spec.decider.id`). Spec validators refuse
`personal_skills` with `skills_declared_only`, `write` without authoring, and `mode: publish`
without an approvals rule covering both `create_skill` and `update_skill`.

## Security controls

| Control | Where | Fails |
|---|---|---|
| A failing scan (critical/high finding) blocks every publish and rollback | `publish_gate.py:policy_reasons`, `PublishPolicy.security_fail_blocks` | closed, not configurable |
| Policy only tightens: tenant `skill_policy` row over `FELIX_SKILL_PUBLISH_*` | `publish_gate.py:publish_policy` | closed |
| Imported text, and anything built on it, blocks on an advisory scan too | `publish_gate.py:policy_for_source`, `carries_imported_text` | closed |
| Catalog serves only `live_version`, and only bytes matching the saved digest | `loader.py:_library_skill` | closed: skipped and logged |
| Library unreadable → catalog without library skills | `loader.py:_library_catalog` | closed per library |
| Host names cannot be saved; an import may not share an operator upload's name | `library.py:host_owns`, `save_draft` | closed |
| Imported skills' tool output marked untrusted; marker scan installed even with screening off | `tools.py` `_relayed`, `manifests/builder.py:apply_content_screening` (`imported_skills`) | closed: quarantine |
| Imported description with injection markers withheld from the catalog | `types.py:Skill` (`listed_description`) | closed |
| Agent copying imported text inherits its lineage | `library.py:_lineage_import`, `copy_rule.py` | closed |
| Unknown version source treated as needing review | `sources.py:needs_review_when_agent_edits` | closed |
| Import allowlist; malformed entry, or a token with no list or with unbound/owner-globbed entries, refuses boot (outside a `make dev` box) | `github.py:check_allowed`, `felix/config.py:_validate_skill_import` | closed |
| Import pinned to one commit, blobs checked by git id, egress-pinned client | `github.py` | closed |
| First-seen cooldown on Felix's clock, never a commit date | `importer.py:cooldown_for`, `sighting_store.py` | closed: `too_recent` |
| Per-tenant and deployment GitHub call budgets | `importer.py:github_call_budget` | closed: 429 `rate_limited` |
| Imports and improvements are drafts; `publish: true` on import is 422 | `routes/skill_import.py`, `improve.py` | closed |

`plugin.json`'s `network.allowed_hosts` is only reported by the scan as a low-severity note
(`security.py:scan_skill_security`); nothing enforces it as egress.

## Stores and their `memory://` twins

Each getter picks the twin when the database URL is `memory://`, `:memory:` or sqlite.

| Getter | Table(s) | Twin |
|---|---|---|
| `store.py:get_skill_activation_store` | `skill_activation` | `InMemorySkillActivationStore` |
| `library_store.py:get_skill_library_store` (`owner=` required) | `skill`, `skill_version`, `skill_file` | `InMemorySkillLibraryStore` |
| `feedback_store.py:get_skill_feedback_store` | `skill_feedback` | `InMemorySkillFeedbackStore` |
| `eval_store.py:get_skill_eval_store` | `skill_eval` | `InMemorySkillEvalStore` |
| `quality_store.py:get_skill_policy_store`, `get_sweep_lease_store` | `skill_policy`, the sweep lease | `InMemorySkillPolicyStore`, `InMemorySweepLease` |
| `sighting_store.py:get_sighting_store` | `skill_import_sighting` | `InMemorySightingStore` |
| `upstream_store.py:get_upstream_store` | `skill_upstream` | `InMemorySkillUpstreamStore` |

Bytes live in the object store under `library_keys.py:library_object_key`. Twins are process
globals: `tests/conftest.py` resets them through `store.clear_memory`, `library_store.clear_memory`,
`quality_store.clear_memory` (which also clears feedback, eval, sighting and upstream) and
`loader.clear_library_skill_cache`.

## Routes, CLI and worker

- `routes/skills.py` — `/skills/{manifest_name}` catalog, one skill, recent activations (`skills:read`).
- `routes/skill_library.py` — `/skill-library` CRUD, versions, files, preview, publish, rollback,
  reject, archive; `org_router` adds `/-/review`, `/-/policy`, `/-/personal`, adopt; the same
  `router` is mounted again at `/skill-library/~{library}` for personal libraries, and `me_router`
  adds promote there.
- `routes/skill_import.py` — `/-/browse`, `/-/import`, `/-/upstream`, `/{name}/-/upstream`, `/{name}/-/update`.
- `routes/skill_quality.py` — feedback (file, list, accept, reject) and evals (queue, list, get).
- `routes/_skill_library_http.py:library_request` decides scope: `skills:read` / `skills:write`
  for the tenant's library, `~me` needs a caller with a personal owner and `skills:personal` to
  write, `~<digest>` is `admin` only. Refusals map `SkillLibraryError.code` through `STATUS`.
- `felix skills browse|add|outdated|diff|update|adopt` (`felix_cli/skills.py`) talks to a running
  server; it never reaches GitHub itself. `felix doctor` adds a note when upstream checks or update
  webhooks are on, since the worker needs the same settings as the API.
- Worker (`felix_worker/tasks.py`): `skill_jobs` (`jobs.py:run_skill_jobs`), `skill_upstream_checks`
  (a no-op at `FELIX_SKILL_IMPORT_CHECK_HOURS=0`) and `skill_update_notifications`; schedules in
  `EXPECTED_SCHEDULES` (`tests/unit/test_worker_cron_tasks.py`).

## Tests

`tests/unit/test_skill_*.py`, `test_skills_*.py`, `test_personal_skill*.py`, `test_cli_skills.py`;
e2e in `tests/e2e/test_skill_import.py`, `test_skill_authoring.py`, `test_personal_skills.py`,
`test_skill_quality_loop.py`, `test_skill_upstream.py`, `test_skill_suggestion.py`. Conformance:
`tests/conformance/test_skill_library_store.py` (also sightings and upstream),
`test_skill_quality_store.py`, `test_skill_caps.py` (exact caps under racing requests). Shared
fakes: `tests/support/skill_import_fake.py` (GitHub at the transport) and `tests/support/skill_quality.py`.

```bash
./scripts/test.sh tests/unit -k skill -q
./scripts/test.sh tests/e2e/test_skill_import.py tests/conformance/test_skill_library_store.py -q
```

## Changing the skill library

1. A new refusal is a `SkillLibraryError` subclass with a stable `code`, plus an entry in
   `routes/_skill_library_http.py:STATUS`; `test_every_library_error_code_has_a_status` fails otherwise.
2. A new version source is a row in `sources.py:SOURCES`, never a membership set elsewhere.
3. A new store needs a Protocol, Postgres and in-memory classes selected like the others, a reset in
   a `clear_memory` the conftest calls, an Alembic revision under `migrations/versions/`, and a
   conformance arm.
4. A publish rule only tightens; route it through `publish_gate.py` so preview and publish agree.
5. Anything new the loader reads must stay digest-checked, live-only and fail-closed.
6. A new `FELIX_SKILL_*` setting goes in `felix/config.py`, `.env.example` and the README.
7. A route or response change: `make contract` and read the diff. A manifest field: `make schema`.
