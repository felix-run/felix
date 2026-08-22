#!/bin/bash
# SessionStart(compact): re-inject the invariants most likely lost in a summary.
cat <<'TXT'
Felix invariants to keep after compaction:
- Tests: ./scripts/test.sh (memory:// stores). Full gate: make check (ruff + ty + pytest + format check); CI types only 'packages apps'.
- Governance wrapper order in manifests/builder.py is load-bearing: secret masking -> policies -> command screening -> content screening -> limits -> guardrails -> judges -> approvals -> artifact spill.
- Core never imports optional plugin packages; apps/api/src/felix_api/composition.py is the only place plugins are named (tests/unit/test_plugin_boundary.py enforces it).
- Keep the default install/image lean: heavy deps live behind extras and are imported lazily inside functions.
- New FELIX_ setting => felix/config.py + .env.example + README table.
- No Cloudflare Workers/DO/Hyperdrive compute in this stack.
TXT
exit 0
