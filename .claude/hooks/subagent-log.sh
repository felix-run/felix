#!/bin/bash
# SubagentStop: append a one-line audit trail of delegated work.
root="${CLAUDE_PROJECT_DIR:-.}"
mkdir -p "$root/.claude/logs" 2>/dev/null || exit 0
INPUT=$(cat)
name=$(printf '%s' "$INPUT" | jq -r '.agent_name // .subagent_type // "subagent"')
sid=$(printf '%s' "$INPUT" | jq -r '.session_id // "-"')
printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sid" "$name" >> "$root/.claude/logs/subagents.log"
exit 0
