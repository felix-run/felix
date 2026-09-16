**Inbound screening reported success and changed nothing on a multimodal turn.** A message
carrying images holds its text in `content_blocks`, both wire formats prefer those over
`.content`, and screening wrote only `.content` — so PII redaction and the `[quarantined]`
substitution ran, were audited as applied, and the model was still shown the caller's original
text. Nothing in `packages/harness` read `content_blocks` at all, which is why no test could see
it. The screened text now replaces the text blocks, with the images preserved in order; blocks
are rebuilt only when screening actually changed something, so an ordinary turn is untouched.
