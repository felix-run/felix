**`spec.plan_execute.planner_few_shots` is gone.** It named a count of planner examples
with no corpus behind it anywhere in the tree, so there was nothing that could make it mean
anything — and inventing a set of examples to justify a field is the wrong way round. It was
read by nothing, which is what makes dropping it inert.

A stored manifest that sets it keeps loading: the field is listed in
`manifests/compat.py:RETIRED`, so it is stripped on read with a warning naming it rather
than failing the manifest under `extra=forbid`. Delete the line and re-save to clear the
warning. Nothing else changes, because nothing ever read the value.
