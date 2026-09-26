#!/bin/bash
# Stop hook: documentation-drift gate. If this session changed a documented surface but
# nothing doc-side moved, block the stop ONCE per drift-set and make Claude either sync
# the docs or say why none is needed. Human oversight, not silence.
#
# "This session" is measured against the snapshot session-start.sh took, so edits the
# tree already carried when the session began never count. A tree with no snapshot -- a
# worktree the session created, say -- is measured against HEAD, which is right for a
# fresh worktree and falls back to the old behaviour otherwise.
input=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
[ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false')" = "true" ] && exit 0

# shellcheck source=lib/surfaces.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/surfaces.sh"

# The tree this session is working in, which is not `CLAUDE_PROJECT_DIR` when the session
# runs in a git worktree -- an everyday shape here. Reading the project root reported
# *another* session's changes as this one's. `cwd` is on every hook payload, Stop included.
workdir=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
[ -n "$workdir" ] || workdir="${CLAUDE_PROJECT_DIR:-.}"
top=$(git -C "$workdir" rev-parse --show-toplevel 2>/dev/null) || exit 0
sid=$(printf '%s' "$input" | jq -r '.session_id // "nosession"')

now=$(drift_snapshot "$top")
[ -z "$now" ] && exit 0
base=$(drift_baseline_file "$sid" "$top")
if [ -f "$base" ]; then
  # Lines in `now` that are not in the baseline verbatim: new files, and files whose
  # content moved since the session started.
  mine=$(printf '%s\n' "$now" | grep -vxF -f "$base" | cut -f1)
else
  mine=$(printf '%s\n' "$now" | cut -f1)
fi
[ -z "$mine" ] && exit 0

surfaces="" docs=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  surface_is_doc "$f" && docs=1
  surface_blocks "$f" && surfaces="$surfaces$f
"
done <<EOF_FILES
$mine
EOF_FILES
[ -z "$surfaces" ] && exit 0
[ "$docs" = 1 ] && exit 0

hash=$(printf '%s' "$surfaces" | shasum | cut -c1-12)
state="${TMPDIR:-/tmp}/felix-docdrift-$sid"
grep -qs "$hash" "$state" 2>/dev/null && exit 0
echo "$hash" >> "$state"

files=$(printf '%s' "$surfaces" | head -8 | tr '\n' ' ')
jq -cn --arg r "Doc-drift check: this session changed documented surfaces ($files) but no README.md / CLAUDE.md / CHANGELOG.md / .env.example / docs/ / deploy/GOVERNANCE.md change came with it. Either (a) update the in-repo docs (user-visible behaviour also gets a CHANGELOG [Unreleased] entry), and use the docs-sync skill for the public MDX pages in the felix-web repo, or (b) state plainly why no documentation change is needed. Fires once per drift-set per session." \
  '{decision:"block", reason:$r}'
exit 0
