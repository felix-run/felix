**`contributor.yaml` runs the gates it used to only name.** Version 2 binds the governed shell as
`run` — the repository's gates and the git verbs that stay inside the checkout, no push, no `gh`,
no bare python — and drops the one-shot Python sandbox. Approvals move from every file write to
publication: `push_files` and `create_pull_request` pause for a person, and the approval row's
arguments are the diff about to leave the machine. Anonymous callers are refused, as the shell
tool requires. The prompt is rewritten around the contract in `docs/SELF.md`: one `felix:go`
ticket, paste the gate output, open a draft, never merge.
