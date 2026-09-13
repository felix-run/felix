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

**Tracebacks are covered too, by indenting each line and then escaping it.** They were the
half a message escape does not reach: `logging.Formatter` appends `exc_text` after the
message, and an exception's own `str` is not indented the way its frames are — it renders at
column 0, so a newline inside an exception message produced a fully record-shaped line on any
of the ~30 `exc_info=True` call sites whose exception text is built from a caller-influenced
value.

Escaping the *block* would have closed that by flattening the traceback onto one line, which
is unreadable and the reason it was left open. Splitting first and escaping each line keeps
the shape and closes the hole: no text is dropped, an operator still reads what the exception
said, and only the record itself begins at column 0. Escaping as well as indenting matters
because indentation is only a claim about columns — `\x1b[1G` is cursor-horizontal-absolute,
so an ESC reaching a traceback redraws that line at column 0 however far right it was
written. **Frame lines now sit two columns further right than a stock Python traceback, and a
tab inside a frame's source line renders as `\t`** — the visible changes to existing logs.

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
