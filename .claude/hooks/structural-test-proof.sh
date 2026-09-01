#!/bin/bash
# PostToolUse(Edit|Write): a structural test just gained a case. Say how to prove it can fail.
#
# The tests that scan the tree — AST walks, rglob corpora, regex over source — are the ones
# that go vacuous without anyone noticing. They pass, they are fast, and they report nothing,
# which reads identically to "the rule holds". This repo has shipped two: an invariant that
# matched `timeout=<Constant>` while every literal it hunted lived inside `httpx.Timeout(...)`,
# and a wrapper-config check written against a hand-maintained list naming six of nine
# wrappers. Both were green on the day they were written and could not have failed.
#
# Behavioral tests are excluded deliberately. They assert on a value the system produced, so a
# broken one usually announces itself; and they are added constantly here, so firing on them
# would make this note ambient noise instead of a signal. Scanning tests are rarer and the
# failure mode is specific to them.
#
# Advisory, never a block: this asks for diligence rather than preventing damage.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
command -v git >/dev/null 2>&1 || exit 0

path=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -n "$path" ] || exit 0
case "$(basename "$path")" in test_*.py) ;; *) exit 0 ;; esac
[ -f "$path" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-.}"
# GIT_DIR/GIT_WORK_TREE in the environment win over -C, which would make every query below
# answer about an unrelated repo. Same reasoning as pr-quality-gate.sh.
git() { env -u GIT_DIR -u GIT_WORK_TREE git "$@"; }
rel=$(git -C "$root" ls-files --full-name --cached --others -- "$path" 2>/dev/null | head -n 1)
[ -n "$rel" ] || exit 0

# Is this a scanning test at all? The three ways this repo builds a corpus.
# The `(^|[^_[:alnum:]])` prefix guard belongs only on the arms that start with a bare
# identifier -- it stops `myast.walk(` and `chaos.walk(`. Applying it to `\.rglob\(` and
# `\.glob\(` too was wrong and silently disabled both: those already begin with `.`, and the
# character before that dot is the receiver's last letter, which is alphanumeric. So
# `ROOT.rglob("*.py")` -- the form every scanning test in this repo actually writes -- never
# matched, and three real ones were invisible to this hook. The fixtures that were supposed to
# prove each arm used `Path(".").rglob(...)`, whose `)` satisfies the class, so the test agreed
# with the bug. Derived coverage in the test file now catches this shape directly.
grep -qE '((^|[^_[:alnum:]])(ast\.(walk|parse)|os\.walk\()|\.rglob\(|\.glob\()' "$path" || exit 0

# Which cases are new relative to HEAD? A file absent from HEAD is entirely new.
# This greps text rather than parsing, so a `def test_…` inside a fixture string counts as a
# case and can appear in the list. Left as is deliberately: a hook must stay fast and must not
# import the tree it is reporting on, and the imprecision is one extra name in a note — it
# cannot change the fire/stay-quiet decision, which is what the tests pin.
now=$(grep -oE '^\s*(async )?def (test_[A-Za-z0-9_]+)' "$path" | grep -oE 'test_[A-Za-z0-9_]+' | sort -u)
[ -n "$now" ] || exit 0
before=$(git -C "$root" show "HEAD:$rel" 2>/dev/null \
  | grep -oE '^\s*(async )?def (test_[A-Za-z0-9_]+)' | grep -oE 'test_[A-Za-z0-9_]+' | sort -u)
added=$(comm -23 <(printf '%s\n' "$now") <(printf '%s\n' "$before"))
[ -n "$added" ] || exit 0

count=$(printf '%s\n' "$added" | wc -l | tr -d ' ')
first=$(printf '%s\n' "$added" | head -n 1)
list=$(printf '%s\n' "$added" | sed 's/^/  - /')

jq -cn --arg ctx "$(cat <<EOF
$rel scans the tree (AST/glob) and gained $count case(s):

$list

A scanning test that matches nothing passes, and reads exactly like the rule holding. Two in
this repo did: one matched \`timeout=<Constant>\` while every literal it hunted lived inside
\`httpx.Timeout(...)\`, and one checked a hand-written list naming six of nine governance
wrappers. Both were green the day they were written.

Prove this one can fail, by mutation — the only method that works for a scan:

  1. Introduce a real violation of the rule in the tree (a probe file under the scanned
     root is usually enough, and leaves nothing to revert but one \`unlink\`).
  2. Run \`./scripts/test.sh $rel::$first\`.
  3. It must go RED. If it stays green the matcher sees a form the real code does not use.
  4. Remove the violation and confirm the tree is clean.

\`./scripts/prove-fails.sh\` does NOT help here. It shadows PYTHONPATH, so it changes what
\`import\` resolves and nothing on disk — a test that reads the tree sees your working copy at
every base. Reach for it when the new case drives code through an import instead.

Then check the corpus and the match set are non-empty: assert the file set has members and
that the check examined candidates, so the day the scan stops finding anything is the day it
fails rather than the day it goes quiet.
EOF
)" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
exit 0
