**Log injection is now closed by the text formatter, not by remembering to wrap a value.**
A newline in a logged value ends the record and starts one the attacker wrote in full — a
forged line that reads as a *refusal* is the damaging case, because the log is what an
incident gets reconstructed from. The fixes for this had all been per call site: escape
this value, then the next one someone finds. That closes instances and never the class,
and the list of call sites only grows.

`FELIX_LOG_FORMAT=json` never had the problem — `json.dumps` escapes the separator because
the message is a *value* there, not a line — so the exposure was the text format alone.
Text now escapes the caller's message before rendering it: whatever was interpolated, the
message is one line. The JSON format is left alone rather than double-escaped.

Tracebacks are deliberately **not** escaped, so they stay multi-line and readable — and
that leaves a hole worth naming rather than implying is closed. `logging.Formatter` appends
`exc_text` after the message, and an exception's own `str` renders at column 0 rather than
indented like its frames, so a newline inside an exception message still produces a
record-shaped line. That is long-standing stdlib behaviour, not something this change
introduced or worsened; the mitigation is `loggable()` on the value before it reaches the
exception, and the boundary is now pinned by a test instead of left to be rediscovered.

The escape is applied to a copy of the record, and deliberately not in a `logging.Filter`,
which is the shorter-looking option the stdlib docs invite: one record is shared by every
handler attached, so escaping there would corrupt a JSON handler's output in order to fix a
text handler's bug. A newline is a separator in one format and an ordinary character in the
other, so the escape belongs where the grammar is chosen.

`felix.manifests.compat.one_line` is gone; `logging_setup.loggable` is the one helper for
this job. The two were not identical — `one_line` escaped every non-printable character and
`loggable` only the C0 range and DEL — so `loggable` picked up the stricter behaviour
rather than the shorter one. It now escapes U+2028, U+2029 and U+0085, which end a line for
a JavaScript-based log viewer even when `tail` shows one, and the bidi overrides, which
reorder a record's visible text without changing a byte of it. Call sites keep using
`loggable`: the formatter cannot truncate, so the bound on an attacker-influenced string
still lives there.
