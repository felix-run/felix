## Summary

<!-- What and why (1–3 bullets). For a Felix-authored PR every section below is required; see docs/SELF.md. -->

-

Closes #

## Evidence re-verified at HEAD

<!-- The ticket's evidence, re-fetched at the commit this branch started from. Still reproduces? If not, close the ticket, not this PR. -->

## Changelog

<!-- The release writes CHANGELOG.md from this section (scripts/changelog.py); CI checks it.
Under a Keep a Changelog heading — Added, Changed, Deprecated, Removed, Fixed, Security —
one `- ` entry per change, for an operator: a bold lead, then what they see now. Or, when
nothing here is user-visible: `none: <reason>`. Do not edit CHANGELOG.md. -->

### Fixed

- **

## Gates run

<!-- Each gate with the literal tail of its output. Never describe a gate you did not run as passed. -->

```
```

## Not verified

<!-- Which gates or claims were not checked, and why. "none" if everything above ran. -->

## Companions

- [ ] `make check` (or `uv run ruff check . && ./scripts/test.sh`)
- [ ] Touched settings documented in `.env.example` / README if applicable
- [ ] Compose / Helm notes updated if deploy behavior changed
- [ ] `## Changelog` above, `make schema`, felix-web page, `docs/OBSERVABILITY.md` — whichever the ticket named

Felix-Thread:
