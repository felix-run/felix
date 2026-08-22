#!/bin/bash
# Status line: branch, dirty count, model, and whether the API is up locally.
input=$(cat)
model=$(printf '%s' "$input" | jq -r '.model.display_name // "claude"')
dir=$(printf '%s' "$input" | jq -r '.workspace.current_dir // "."')
cd "$dir" 2>/dev/null || exit 0
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "-")
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
api=""
curl -fsS -m 1 http://localhost:8080/health >/dev/null 2>&1 && api=" | api:up"
printf 'felix %s | %s%s%s' "$model" "$branch" "$([ "$dirty" != "0" ] && echo " *$dirty")" "$api"
