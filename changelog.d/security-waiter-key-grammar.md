**One thread could forge another's client-tool waiter key.** A waiter name is a *key* —
whoever can construct it can answer the wait behind it — and
`f"client:{thread_id}:{tool_call_id}"` was not injective, because both parts may contain the
separator. `thread_id` carries colons legitimately (`{tenant}:{suffix}`, and
`{tenant}:fiber:{id}` for a durable run), and `tool_call_id` arrives off the model wire with
no charset check at all (`wire/openai_completions.py` takes `str(tc.get("id") or "")`).

Concretely, and reachable rather than theoretical:

```
thread acme:fiber:F123, call call_9        ->  client:acme:fiber:F123:call_9
thread acme:fiber,      call F123:call_9   ->  client:acme:fiber:F123:call_9
```

`fiber` is a legal thread suffix — `effective_thread_id` rejects only `:` and `#` — so the
second thread is one any caller in that tenant can create. Posting a `tool_result` for it
resolved the durable run's pending client tool with content the poster chose. Same tenant
only: the tenant prefix cannot be forged, since a tenant id carrying the delimiter is refused
outright.

Waiter names are now composed by `waiters.waiter_name`, which percent-encodes each part (`%`
before `:`, so the escape cannot itself be forged) before joining. The approval and UI prompt
waiters go through it too — their ids are a `uuid4().hex` and a `token_urlsafe`, so they were
never ambiguous and their names are **byte-identical** to before; routing them through one
helper is so the next part added to a waiter name is escaped by construction rather than by
whoever remembers.

**Upgrade note.** Client-tool waiter names change shape, so a client-tool call already in
flight across a rolling upgrade will not be answered by the new process and times out after
`DEFAULT_TIMEOUT_SECONDS` (120s), returning `[error/timeout]` to the model. That is the
fail-closed direction and it resolves itself on the next call; approvals and UI prompts are
unaffected because their names did not change.
