**`spec.mcp_servers[].tools` allowlists which remote tools bind.** Glob patterns over the remote
names; empty keeps the old behaviour of binding the whole catalogue. A non-empty list is what
makes a server's mutating surface enumerable — an approval rule can only gate a tool it can name,
and until now a write tool the server added overnight bound ungated with nothing to go red.
`contributor.yaml` uses it, and its test now proves every bound GitHub write tool is gated
instead of comparing the manifest to itself. A pattern the server no longer serves is logged at
bind time, not refused.
