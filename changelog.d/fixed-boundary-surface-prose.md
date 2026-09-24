**The boundary check reads the path, not the sentence.** A ticket's `Files expected to change`
lines are written as `- `path` — why`, and the first Felix-authored pull request (#290) failed
the surface check on every file because each whole line was read as a glob. The path is now the
first backticked span or path-shaped token on the line; the rest is commentary.
