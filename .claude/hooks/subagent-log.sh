#!/bin/bash
# SubagentStop: append a one-line audit trail of delegated work.
#
# The payload names the agent `agent_type` (and `agent_id`). This read `.agent_name` and
# `.subagent_type`, which it never carries, so every line in the log said "subagent" and
# the trail recorded that something ran and nothing about what.
root="${CLAUDE_PROJECT_DIR:-.}"
mkdir -p "$root/.claude/logs" 2>/dev/null || exit 0
INPUT=$(cat)
command -v jq >/dev/null 2>&1 || exit 0
name=$(printf '%s' "$INPUT" | jq -r '.agent_type // .agent_name // .subagent_type // "subagent"')
aid=$(printf '%s' "$INPUT" | jq -r '.agent_id // "-"')
sid=$(printf '%s' "$INPUT" | jq -r '.session_id // "-"')
printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sid" "$name" "$aid" >> "$root/.claude/logs/subagents.log"
exit 0
