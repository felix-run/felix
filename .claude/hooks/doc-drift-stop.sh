#!/bin/bash
# Stop hook: documentation-drift gate. If this session changed a documented
# surface but nothing doc-side moved, block the stop ONCE per drift-set and make
# Claude either sync the docs or say why none is needed. Human oversight, not silence.
input=$(cat)
[ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false')" = "true" ] && exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
changed=$({ git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } | sort -u)
[ -z "$changed" ] && exit 0

surfaces=$(printf '%s\n' "$changed" | grep -E '^(apps/api/src/felix_api/routes/|packages/harness/src/felix/(manifests/schema|manifests/builder|config|sdk)\.py$|packages/harness/src/felix/(auth|governance|patterns)/|packages/cli/src/felix_cli/main\.py$|migrations/versions/|deploy/)')
[ -z "$surfaces" ] && exit 0

# Any doc-side change in this repo counts as "docs were considered".
printf '%s\n' "$changed" | grep -qE '^(README\.md|CLAUDE\.md|CHANGELOG\.md|\.env\.example|docs/|deploy/GOVERNANCE\.md|deploy/.*/README\.md)$' && exit 0

sid=$(printf '%s' "$input" | jq -r '.session_id // "nosession"')
hash=$(printf '%s\n' "$surfaces" | shasum | cut -c1-12)
state="${TMPDIR:-/tmp}/felix-docdrift-$sid"
grep -qs "$hash" "$state" 2>/dev/null && exit 0
echo "$hash" >> "$state"

files=$(printf '%s\n' "$surfaces" | head -8 | tr '\n' ' ')
jq -cn --arg r "Doc-drift check: this working tree changes documented surfaces ($files) but no README.md / CLAUDE.md / .env.example / docs/ / deploy/GOVERNANCE.md change came with it. Either (a) update the in-repo docs, and use the docs-sync skill for the public MDX pages in the felix-web repo, or (b) state plainly why no documentation change is needed. Fires once per drift-set per session." \
  '{decision:"block", reason:$r}'
exit 0
