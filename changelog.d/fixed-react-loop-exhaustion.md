**A react loop that runs out of steps says so.** It used to fall through with the model's own
`tool_use` and a session status of complete, so a run cut off mid-thought looked finished — the first
live triage run stopped at step ten with two tool calls pending and nothing recorded it. It now ends
with `stop_reason: max_turns`, status `truncated`, and `felix_run_stop_reason{reason="max_turns"}`.
Related: `spec.max_turns` never bounded a react agent — `spec.recursion_limit` does — and three
bundled manifests carried it anyway; they set `recursion_limit` now, and the compile step warns when
a single-agent manifest sets one without the other.
