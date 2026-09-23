**Eval rubrics can score what a run did, not only what it said.** `tools_called`,
`tools_not_called`, `max_tool_calls` and `max_errors` are read off the run's messages — and off
`mock_tool_calls` / `mock_tool_errors` under `--mock`, so the counter-smoke can show each one
rejecting. A run row now carries `error_count`, the subset of `fail_count` that never reached the
scorer, so a malformed dataset reads differently from a model regression.
`fixtures/eval/contributor.json` is the first dataset that scores the agent itself.
