**`felix-boundary` is the gate a Felix-authored pull request cannot edit its way past.** A
`pull_request_target` workflow runs the base branch's `scripts/felix_boundary.py` — no head
checkout — and fails a bot-authored PR that touches the files constraining Felix (`.github/`,
`.claude/`, the governance and security packages, `builder.py`, the shell tool, migrations,
deploy, its own manifests, this script), lacks the PR contract, closes a ticket no person marked
`felix:go`, or changes paths the ticket did not name. A person's pull request passes without
being looked at.
