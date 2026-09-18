**A streamed composite run now reports how it really ended.** The `done` event that
`deep`, `router`, `parallel`, `groupchat`, `reflect` and `plan_execute` emit carried the
final message and not the `stop_reason` beside it, so `/v1/chat/completions` — which fills
`finish_reason` from exactly that field — reported the default for every streamed composite
turn, including one the model truncated on `max_tokens` or the provider refused. The
non-streaming path was always correct, which is why the gap survived: the same run answered
honestly through `invoke` and vaguely through SSE. A reply-guard denial was the one case
that already came out right, because `ReplyControlsAgent` rewrites the event it rewrote the
reply on — and only a manifest with reply controls configured had that.

The same field is read internally. `_pipe_stream` keeps the *last* terminal event it sees,
so a composite delegating to another composite recorded `end_turn` however the child really
ended — which meant `plan_execute` would replan a refused subtask on the `invoke` path and
not on the streamed one.
