**A turn can now name an uploaded file instead of carrying it.** `POST /files` stored bytes
and handed back a `file_id`; nothing consumed one. A message may now include OpenAI's own
`{"type": "file", "file": {"file_id": "…"}}` content part, and the harness expands it to the
stored bytes immediately before the model call.

The expansion is late on purpose, and that timing is the whole feature. An image sent inline
lands in the session event log, and `full_replay` re-sends that log on every later turn — so
a 600 KiB screenshot attached on turn one is re-uploaded to the model on turns two, three
and four. Expanding at ingest would have put the base64 in the log and paid that cost
silently. The log keeps the reference; the bytes are fetched per turn.

The reference rides in the same `url` field an inline image uses, as a `felix-file://` URI,
rather than in a new field. The session layer already persists and restores `attachments[].url`
and both wires already read it — a parallel `file_id` field would have had to be threaded
through each of those, and the one that would have been missed is the session layer, which is
the only path a *second* turn takes. Nothing would have failed until replay.

The tenant comes from the caller's credentials and never from the reference, so naming
another tenant's `file_id` is indistinguishable from naming one that never existed. The media
type is read back off the bytes rather than from a stored label, because the default
filesystem store discards the label — and the sniff now checks a RIFF container's format
tag, since `RIFF` alone is also WAV and AVI and this is the only thing deciding what a model
is told these bytes are.

A reference that cannot be resolved — deleted since, another tenant's, or bytes that are no
longer a type any wire encodes — is dropped with a warning naming the id rather than raised:
the turn naming it is already in an append-only log, so refusing would make a thread
unanswerable for good the moment an attachment was deleted. Where dropping would leave the
turn with nothing to send, a short marker replaces it instead. A turn whose only content part
was the reference would otherwise reach the provider with empty content, which is itself an
error and would wedge the thread just as permanently by the other road; the marker also stops
the model answering confidently about an image it was never shown. The id appears in that
marker only when it is well formed, because that text reaches the model.

The same id repeated across a replayed context is read once per model call.

Unchanged, and worth stating because the roadmap left it open: resolution happens **after**
`apply_inbound_screening`, which is where it has to be if the log is to keep the reference.
That is not a regression in screening coverage — `_message_text` collects only blocks of type
`text`, so image content has never reached a screener, inline `data:` URLs included. Text
rendered inside an uploaded image remains an injection channel that screening does not see,
exactly as it was for inline images.
