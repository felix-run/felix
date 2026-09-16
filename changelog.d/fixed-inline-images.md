**An image sent inline reached OpenAI and 400'd on Anthropic.** A `data:` URL is how OpenAI's
own API documents attaching an image, so it is what every SDK emits — and the Anthropic wire put
it in a `url` source, which that API rejects. Both wires had an image encoder and only one of
them worked, on the provider this harness defaults to. Inline images now go to Anthropic as a
`base64` source, labelled with the media type out of the data URL rather than the `image/png`
the parser fills in for anything unlabelled; a remote `https://` image still goes as a URL, which
is the form that exists so the provider fetches it itself. The older `attachments` shape carried
the same URLs and the same bug, and now takes the same path.

A percent-encoded data URL — `data:image/svg+xml,<svg …>`, which is a legal and ordinary way to
write an image inline — is re-encoded as base64 rather than dropped: neither provider accepts the
percent-encoded form, so it was a hard 400 on one route and a confidently wrong answer on the
other, decided by nothing but which model the manifest routed to. Images render on a user turn
only, which is the sole place either API accepts one; the OpenAI wire used to send one on an
assistant turn, which is reachable by replaying a vision thread. Both wires now render one
normalised part list, so a message's two shapes — `content_blocks` on the turn that parsed it,
`attachments` on every turn replayed out of the session log — cannot drift apart again. A content
part of a type Felix does not recognise is still dropped, but is now logged rather than vanishing.
