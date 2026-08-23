#!/bin/bash
# PostToolUse(Edit|Write|MultiEdit): quality ratchet. Measures the edited .py with
# stdlib ast and compares it to its HEAD version, speaking only when the edit made
# a metric worse than it already was. A file that is over budget and did not get
# worse stays silent, so large-by-design modules never nag. Advisory, never blocks.
#
# Budgets (mirrored in the code-quality skill): function body 60 lines,
# nesting depth 4, parameters 7 (excluding self/cls), module 600 lines.
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
fp=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$fp" ] && exit 0
case "$fp" in *.py) ;; *) exit 0 ;; esac
case "$fp" in */.venv/*|*/node_modules/*|*/site-packages/*) exit 0 ;; esac
[ -f "$fp" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-.}"
cd "$root" 2>/dev/null || exit 0
rel="${fp#"$root"/}"
# Tests, generated migrations, and one-off scripts are judged by a reviewer, not a budget.
case "$rel" in tests/*|migrations/*|scripts/*|/*) exit 0 ;; esac

command -v git >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0

report=$(python3 - "$rel" <<'PY' 2>/dev/null
import ast
import subprocess
import sys

FUNC_LINES, NESTING, PARAMS, MODULE_LINES = 60, 4, 7, 600
BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
if hasattr(ast, "Match"):
    BLOCKS = BLOCKS + (ast.Match,)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def depth(node, level=0):
    best = level
    for child in ast.iter_child_nodes(node):
        if isinstance(child, DEFS + (ast.ClassDef,)):
            continue
        best = max(best, depth(child, level + (1 if isinstance(child, BLOCKS) else 0)))
    return best


def params(node):
    a = node.args
    positional = list(a.posonlyargs) + list(a.args)
    count = len(positional) + len(a.kwonlyargs)
    if positional and positional[0].arg in ("self", "cls"):
        count -= 1
    return count


def collect(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    found = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            elif isinstance(child, DEFS):
                name = prefix + child.name
                span = (child.end_lineno or child.lineno) - child.lineno
                found[name] = (span, depth(child), params(child))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return len(source.splitlines()), found


rel = sys.argv[1]
try:
    current = collect(open(rel, encoding="utf-8").read())
except OSError:
    sys.exit(0)
if current is None:
    sys.exit(0)  # a syntax error is ruff-format.sh's report to make, not ours

try:
    head = subprocess.run(
        ["git", "show", "HEAD:" + rel], capture_output=True, text=True, timeout=10
    )
    previous = collect(head.stdout) if head.returncode == 0 else None
except (OSError, subprocess.SubprocessError):
    previous = None

cur_lines, cur_funcs = current
prev_lines, prev_funcs = previous if previous else (None, {})

findings = []
if cur_lines > MODULE_LINES and (prev_lines is None or cur_lines > prev_lines):
    was = "new file" if prev_lines is None else "was %d" % prev_lines
    findings.append("module is %d lines (%s, budget %d)" % (cur_lines, was, MODULE_LINES))

labels = (("body lines", FUNC_LINES), ("nesting depth", NESTING), ("parameters", PARAMS))
for name, values in sorted(cur_funcs.items()):
    before = prev_funcs.get(name)
    for index, (label, budget) in enumerate(labels):
        now = values[index]
        if now <= budget:
            continue
        old = before[index] if before else None
        if old is not None and now <= old:
            continue  # already over budget, but this edit did not make it worse
        was = "new" if old is None else "was %d" % old
        findings.append("%s(): %s %d (%s, budget %d)" % (name, label, now, was, budget))

for line in findings[:4]:
    print(line)
if len(findings) > 4:
    print("... and %d more" % (len(findings) - 4))
PY
)

[ -z "$report" ] && exit 0

jq -cn --arg ctx "Quality ratchet — $rel crossed a complexity budget on this edit:
$report
Budgets are smoke alarms, not rules: look before rewriting. The extractable seam is usually a comment already in the function. Load the code-quality skill for the rubric, and felix-quality-reviewer for a full pass. If the growth is deliberate and the module is large by design, say so and move on." \
  '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$ctx}}'
exit 0
