**Every hook that judged a file by its repo-relative name was reading the wrong tree.**
They derived that name by stripping `CLAUDE_PROJECT_DIR` off an absolute path — which is
correct in the main checkout and wrong in a git worktree, where the file lives at
`<project>/.claude/worktrees/<name>/<rel>`. The strip left the worktree prefix attached and
every anchored pattern stopped matching.

`protect-files.sh` therefore failed **open**: inside a worktree, `.env`, `uv.lock`,
`secrets/`, generated directories and already-published Alembic revisions were all freely
editable, silently, because `.claude/worktrees/x/.env` does not match the pattern `.env`.
Verified by running the hook, not by reading it.

`quality-ratchet.sh` lost every file's history the same way: `git show HEAD:<rel>` found
nothing, so `previous` was `None`, which both bypasses the "did this edit make it worse"
guard and prints "new file". A ratchet that exists to stay quiet about pre-existing size
became one that reports absolute size on every edit — observed as "module is 696 lines (new
file)" for a module months old, after a twelve-line change.

`doc-drift-stop.sh` inspected `CLAUDE_PROJECT_DIR` directly and so reported *another*
session's changes as this one's, blocking the turn twice in one session over files that
session had never opened. It now reads `cwd` from the hook payload, a documented field on
every event including `Stop`.

Two shared helpers in `lib/command.sh` carry the rule — `hook_repo_root` and
`hook_repo_rel`, deriving the answer from the file's own repository rather than from the
project root — and `tests/unit/test_file_guard_hooks.py` covers the Write/Edit guards in
both trees, which nothing did before.
