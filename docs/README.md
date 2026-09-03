# Felix repo docs

Maintainer and contributor documentation for the **felix-run/felix** repository. Everything here is
about working *on* Felix.

Documentation about *using* Felix — installation, concepts, the manifest reference, the REST,
management, OpenAI-compatible, A2A, and MCP APIs, and the internals write-ups — lives on the public
site at **[docs.felix.run](https://docs.felix.run)**, authored in the separate
[`felix-run/web`](https://github.com/felix-run/web) repo under `apps/docs/src/content/`.

## What is here

| File | Purpose |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | Living tracker of what to build next; status updated in place |
| [`HISTORY.md`](HISTORY.md) | Wave-by-wave record of what shipped and what each wave taught, including the conclusions that did not survive being measured |
| [`RELEASING.md`](RELEASING.md) | Cutting a release: version, changelog, tag, and what CI does |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Local failure modes and the actual fix for each |
| [`UPGRADING.md`](UPGRADING.md) | Moving a deployment between versions: migrations, the settings that must move with them, verification, rollback |

Filenames in this directory are `UPPERCASE.md`.

## Where everything else lives

| Topic | Location |
|---|---|
| Product documentation, guides, API reference, internals | [docs.felix.run](https://docs.felix.run) (`felix-run/web`) |
| Repository overview and quick start | [`../README.md`](../README.md) |
| Contribution workflow and PR expectations | [`../CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Vulnerability reporting | [`../SECURITY.md`](../SECURITY.md) |
| Release history | [`../CHANGELOG.md`](../CHANGELOG.md) |
| Instructions for AI coding agents | [`../CLAUDE.md`](../CLAUDE.md) and [`../.claude/README.md`](../.claude/README.md) |
| Governance controls, SOC 2 / EU AI Act mapping | [`../deploy/GOVERNANCE.md`](../deploy/GOVERNANCE.md) |
| Docker, Helm, AWS, GCP deployment notes | [`../deploy/`](../deploy/) |

## The rule that keeps this directory small

**Anything a Felix *user* needs belongs on the public site, not here.** A page duplicated across both
repos drifts within a release or two, and the copy people find first is usually the stale one. When
you are unsure which side something belongs on, ask whether it would still matter to someone who
never clones this repository — if yes, it goes to `felix-run/web`.

The `docs-sync` skill in [`../.claude/skills/docs-sync/`](../.claude/skills/docs-sync/) maps each code
surface to the public page that documents it, and `references/page-map.md` there is the lookup table.
