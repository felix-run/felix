#!/bin/bash
# SessionStart(compact): re-inject the invariants most likely lost in a summary.
cat <<'TXT'
Felix invariants to keep after compaction:
- The recurring defect shape: a control that looks present and does nothing, because the branch production takes is the branch nothing covers. Exercise the production call (no arguments, the console-script path), not a convenient one.
- A test that cannot fail is worse than no test: prove a new one fails without the change. Method and tooling: .claude/rules/felix-invariants.md.
- Absence rots fastest. Re-grep the tree at HEAD before acting on "nothing reads this" — never on an earlier note in this session.
- Validating a value for one grammar does not validate it for the next: re-check separators when it crosses into a command line, header, URL, or query.
- Tests: ./scripts/test.sh (memory:// stores). Full gate: make check (ruff + ty + pytest + format check) — on a lean venv it skips ty and says so last; use STRICT=1 make check or make check-ci. CI types only 'packages apps'.
TXT
# toolkit:enum wrapper-order (scripts/validate-toolkit.py compares the next line with the code)
echo '- Governance wrapper order in manifests/builder.py is load-bearing: secret masking -> policies -> command screening -> content screening -> limits -> guardrails -> judges -> approvals -> permission mode -> artifact spill -> workspace scope.'
cat <<'TXT'
- Core never imports optional plugin packages; apps/api/src/felix_api/composition.py is the only place plugins are named (tests/unit/test_plugin_boundary.py enforces it).
- Keep the default install/image lean: heavy deps live behind extras and are imported lazily inside functions.
- New FELIX_ setting => felix/config.py + .env.example + README table.
- No Cloudflare Workers/DO/Hyperdrive compute in this stack.
TXT
exit 0
