**`spec.shell_tools` runs an allowlisted argv in the workspace checkout.** The capability the
sandbox does not give a coding agent: `./scripts/test.sh`, `ruff`, `ty`, `git status` against the
files `write_file` just edited. No shell interpreter — `&&` is an argument — and every prefix a
manifest names must be covered by `FELIX_SHELL_ALLOWED_COMMANDS`, which is empty by default and
checked at manifest write, at compile, and per call. The child sees five environment variables,
`cwd` resolves under the workspace root, the run is killed at `timeout_ms`, and output is capped.
