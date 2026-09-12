**A tool named `felix_structured_output` had its call silently swallowed.** The Anthropic wire
reserves that name for structured output and folds a call to it back into the turn's reply — but
the guard against a manifest binding the same name ran only when a schema was requested, while
the fold ran on every turn. A manifest binding it through `spec.client_tools` and declaring no
`output_schema` therefore had that tool never execute, its model-authored arguments returned as
the final answer, and the stop reason forced to `end_turn`, with nothing logged. The fold now
runs only on a turn that asked for a schema.
