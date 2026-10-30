---
name: skill-library
description: How Felix loads, imports, screens and publishes Agent Skills: catalog precedence, tenant and personal libraries, GitHub import into drafts (allowlist, first-seen cooldown), the publish gate, promotion and adopt, and the worker's improvement loop. Use when editing felix/skills/, the `felix skills` CLI, a /skills or /skill-library route, or spec.skills / skill_authoring / personal_skills / skill_suggestion, or when a skill is missing from a catalog, refused, or an imported skill's text is quarantined.
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
| Load | `loader.py`, `discovery.py`, `types.py`, `activation.py`, `tools.py`, `suggest.py` | Host dirs + library + operator uploads → `SkillCatalog`; the four skill tools; per-thread activation read from the session log; the decider hint |
| Format | `format.py`, `binary.py`, `plugin.py`, `semver.py` | SKILL.md parse/validate and bundle path allowlist; base64 assets; `plugin.json`; version bumps |
| Library | `library.py`, `library_store.py`, `library_keys.py`, `sources.py`, `copy_rule.py`, `authoring.py` | Draft/publish/rollback/reject/archive/adopt/promote; rows; owner and object-key spelling; per-source rules; imported-text lineage; agent tools |
| Import | `github.py`, `importer.py`, `sighting_store.py`, `upstream.py`, `upstream_store.py`, `bundle_diff.py`, `update_notify.py` | Pinned GitHub fetch and allowlist; import as draft; cooldown clock; origin checks, diffs, updates; `skill.update_available` webhooks |
| Screen / publish | `format.py`, `review.py`, `security.py`, `publish_gate.py`, `policy.py` | Validation, 0-100 quality score, heuristic scan, the verdict, the tighten-only policy |
| Evolve | `feedback.py`, `feedback_store.py`, `evaluate.py`, `eval_store.py`, `improve.py`, `quality_store.py`, `jobs.py`, `job_limits.py`, `model_calls.py` | Feedback a person decides; baseline-vs-skill evals; rewrite-to-draft; the worker sweep, its lease and per-tenant caps |

## From skill to activation

1. **Bundled or operator skills.** `skills/<name>/SKILL.md` in the repo and `FELIX_SKILLS_DIR` are
   the host catalog (`skills/loader.py:host_catalog`). Discovery (`discovery.py:_skill_dirs`) takes
   only `<dir>/SKILL.md`, treats a skill directory as a leaf, skips hidden dirs and `node_modules`,
   and is bounded (`MAX_SCAN_DEPTH`, `MAX_SCAN_DIRS`). Loading is lenient as agentskills.io's
   client guide asks: a missing description or unreadable YAML skips; a bad name, a name unlike
   its directory, or an over-long description warns and loads; shadowing is logged. Operator
   uploads live at the object-store keys `skills/loader.py:operator_skill_keys` and
   `pinned_operator_skill_keys` spell. `felix skills validate <dir>` runs the strict
   `format.py:validate_skill_bundle` locally.
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
   `read_skill_file` (`SKILL_TOOL_NAMES`). The catalog block is sorted by name. A skill the model
   is not offered (`disable-model-invocation`, or an `unresolved` ref nothing answered) is hidden
   from the tools too (`types.py:SkillCatalog.get_invocable`), and the tools' `name` schema
   enumerates only invocable names. `activate_skill` caps the body (`MAX_ACTIVATION_CHARS`) and
   relays `license`, `compatibility` and `allowed-tools` as information — `allowed-tools` is
   never enforced or used to skip approvals.
6. **Activation is the thread's.** `skills/activation.py:thread_activations` reads it from the
   session log: the newest `activate_skill` result per name on the active branch, minus a later
   `deactivate_skill` -- so a rewind or fork past it, or a result screening quarantined, is not
   active. There is no activation table (dropped in migration `0047`). A second activation of the
   same instructions in a thread answers `already_active`. `session/clearing.py` never stubs an
   `activate_skill` result, the artifact spill skips it (`artifacts.UNSPILLED_SOURCES`), and
   `activation.SkillRetentionStrategy` (composed by `manifests/builder.py` once the catalog is
   loaded) re-shows an active skill's logged result when compaction or a window dropped its turn:
   only skills the catalog still offers, fenced, ahead of the history, within `MAX_RETAINED_CHARS`
   (`activation.retained_view` decides, and `already_active` asks the same function). A queue
   write-back under a skill tool's name is stored as `queue:<name>` (`session/store.py`).
7. **`/name` and `preload`.** `skills/invocation.py:SkillPreamble` (built by the builder, run by
   `patterns/react.py:_run_skill_preamble` before the first model step) turns a run whose last
   message is `/name args` -- `name` a skill the person may run (`SkillCatalog.get_for_user`:
   resolved and `user-invocable`, which a `disable-model-invocation` skill still is) -- and each
   `SkillRef.preload` not already shown in the thread into `activate_skill` calls through the tool
   runner, logged as an assistant turn. The person's turn stays as typed; a declared prompt of the
   same name was already expanded by the route. Only `InvokeInput.person_turn` runs (`/chat`, `/v1`,
   a durable `/chat` run -- never a `task` prompt). `invocation.PersonCalls` mints the batch's call
   ids, and `activate_skill` honours a person's privileges -- a model-hidden skill, `arguments` --
   only for such an id, once; the model's own `arguments` are ignored. `$ARGUMENTS`,
   `$ARGUMENTS[N]` and `$N` are substituted (`arguments.with_arguments`, bounded); a body that never
   names `$ARGUMENTS` gets `ARGUMENTS:` appended. `SkillCatalog.get_activatable` is the predicate for
   an activation already in a thread; the round runs through `react._tool_round`, as the model's do.
8. **Forked skills.** Frontmatter `context: fork` (`Skill.fork`, with `agent` -> `fork_agent`,
   `model` -> `fork_model`; all reserved from nested `metadata:`) makes `activate_skill` run the skill
   in a delegation child instead of returning its body: `tools/delegation.py:ForkRunner`, built by the
   builder over the same children map `task` gets and bound to the *governed* `task` tool once every
   wrapper has wrapped it, calls that tool -- so approvals, policies, limits, permission mode and
   `subagent_stop` hold for a fork as for `task`. The call goes through a `ToolRunner.dispatch`
   (`ForkRunner._dispatch`, hooks from `bind(..., hooks=)`), so `pre/post_tool_use`, plugin tool
   hooks and a `tool_call` row see it; `ToolInvocationCtx.within` tells `apply_limits` it is the
   `activate_skill` call's work, not a second `max_tool_calls` call. A vetted `model:` (route defined, lead's tree allows
   it) reaches the `task` handler through `model_overrides` keyed by a minted call id. The child's
   turn is the model's `task` then `<skill_instructions>` (or the person's substituted arguments); a
   wrapper deny is passed on, an error is `fork_failed`, an answer is untrusted output; a fork is
   never an activation, so nothing is retained and it is never preloaded. An imported skill nobody
   adopted never forks (`fork_refused`). Where forks run is `manifests/builder.py:_fork_runner`: the
   delegation agents, or, with none (and no `sub_agents`), the manifest itself for a fork that names no `agent:`
   or names it (`_SelfFork`: compiled on first use against a copy of the build state marked
   `BuildDeps.self_fork_of`, one level deep, quiet, through a governed `task` withheld from the
   model). An `agent:` outside those, or a fork inside that subagent, is `fork_unavailable`
   (warned at the manifest's own compile).
9. **Skill scripts.** `spec.skill_scripts` (`SkillScriptsSpec`: enabled, `interpreters` by extension,
   timeout_ms) binds `run_skill_script` (`skills/scripts.py:make_skill_script_tool`), transport `shell`. It runs only a bundle
   file under `scripts/` of a skill active in the calling thread and not `untrusted`, under an
   interpreter the operator lists in `FELIX_SKILL_SCRIPT_INTERPRETERS`
   (`security/shell_policy.py:assert_skill_scripts_allowed` at write and compile). The bundle is
   written by descriptor to `.felix-skills/<skill>/<token>/` (a `PANE_HIDDEN_DIRS` entry) and removed
   after. The call execs through `tools/shell.py:exec_in_place` at `shell_place`, and the runner
   admits `kind: skill_script` by `skill_script_argv_issue`. The hosted backend refuses it.

Manifest fields (`manifests/schema.py`): `skills` (`SkillRef`: name, description, version, preload),
`skills_declared_only`, `personal_skills` (`off` / `read` / `write`), `skill_authoring`
(`SkillAuthoringSpec`: enabled, mode `draft`/`publish`, max_pending, auto_eval) and
`skill_scripts` (`SkillScriptsSpec`, above) and `skill_suggestion` (`SkillSuggestionSpec`, needs
`spec.decider.id`). Spec validators refuse
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
| A skill hidden from the catalog cannot be activated or read by a model guessing its name | `types.py:SkillCatalog.get_invocable`, `tools.py` | closed: `unknown_skill` |
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
