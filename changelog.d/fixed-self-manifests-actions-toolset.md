**The self-build manifests can read a failed smoke run, and cache their prompts.** The GitHub MCP
default catalogue carries no actions toolset, so `list_workflow_runs` and its siblings bound nothing
— the first triage run logged exactly that. `triage` and `contributor` now bind from `/mcp/x/all`
with the names that exist (`actions_list`, `actions_get`, `get_job_logs`); the allowlist narrows the
rest away. `model.cache: true` on both: the first run paid $1.04 for ten calls with no cache reads.
