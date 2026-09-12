# Changelog fragments

**Do not edit `CHANGELOG.md` in a pull request.** Add a file here instead.

One file per change, named `<section>-<slug>.md`:

```
changelog.d/fixed-recall-drops-channels.md
changelog.d/added-document-search-tool.md
```

The section is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security` —
the Keep a Changelog set, and the order they are rendered in. The slug is yours; it only
has to be unique, and the branch name usually works.

The body is the entry exactly as it should read in the changelog, without the leading
`- `:

```markdown
**`felix mint-jwt` printed a token you could not use.** It went through rich, which wraps
to the console width, so `TOKEN=$(felix mint-jwt …)` captured seven lines of base64 …
```

## Why a directory instead of the file

Every pull request used to append to the top of the same block in `CHANGELOG.md`, so any
two open at once conflicted there — every time, on a file where a botched resolution
silently drops somebody's entry. That happened: six entries were lost once to a rewrite
that should have been a merge.

Two changes cannot conflict here, because they are never in the same file.

## What to do with it

```bash
make changelog          # what the next release section would say
python3 scripts/changelog.py --check     # names and sections are valid (CI runs this)
python3 scripts/changelog.py --release X.Y.Z   # fold into CHANGELOG.md, delete the fragments
```

`--release` is a release step, not a pull-request step; `docs/RELEASING.md` has it in order.
