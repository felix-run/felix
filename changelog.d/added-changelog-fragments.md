**Changelog entries are files now, so two pull requests cannot conflict over one.** Every
change adds `changelog.d/<section>-<slug>.md` instead of appending to `CHANGELOG.md`, and
`python3 scripts/changelog.py --release X.Y.Z` folds them in when the release is cut.

Appending to the top of one block meant any two open pull requests collided there, every
time. That conflict is worse than most: the resolution is prose, no merge tool helps, and a
botched one silently drops somebody's entry — which happened, six at once, to a rewrite that
should have been a merge. Two fragments are never in the same file.
