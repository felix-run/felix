**Four `spec.plan_execute` fields now do what they say, and a fifth is gone.**
`planner_model`, `executor_model`, `replan_on_failure` and `max_replans` each had exactly
one reference in the tree: their own definition. A manifest could name a planner model, ask
for replanning and cap it, and `felix validate-manifest` would bless all of it while the
harness ignored every one — the only way to learn the truth was to grep the harness.

`planner_model` and `executor_model` route the planning call and the subtask agent
independently of the manifest's model. The asymmetry is the point of a plan/execute split:
planning is one call whose quality shapes everything after it, execution is many narrow
calls, so "plan with the expensive model, execute with the cheap one" is the lever the
pattern exists to offer. Unset keeps the manifest's model, so nothing changes for a spec
that says nothing. Everything else on the spec — price overrides, fallbacks, thinking level
— is carried across: naming a planner route is not opting out of your own model
configuration. `executor_model` is applied where `executor_recursion_limit` already
was — in `_build_plan_execute`, the one place core builds this executor — which is worth
saying because the first attempt applied it in `_DelegatingAgent` instead, on a branch that
is dead for every compiled manifest, and left the field exactly as inert as before.

`replan_on_failure` replans the **remaining** steps when one ends early, bounded by
`max_replans` (`0` disables it as surely as the boolean does). The steps already done stay
done and their notes are carried into the new plan, so a replan does not spend the budget
twice. What counts as "ends early" is deliberately narrow — `refusal`, meaning governance
replaced the reply, and `max_tokens`, meaning the model was cut off mid-answer. In both the
note the synthesiser would record is not an answer. Everything else, including an empty
reply, is a step that ran to completion and produced little: a planning problem rather than
a failure, and replanning on it would loop on subtasks that are simply hard to say anything
about.

`planner_few_shots` was **removed** rather than wired. It named a count of planner examples
with no corpus behind it anywhere in the tree, so there was nothing to make it mean, and
inventing one to justify a field is the wrong way round. It is in `manifests/compat.py`'s
`RETIRED` list, so a stored manifest that set it keeps loading — dropping it is inert
precisely because nothing read it.

`tests/unit/test_inert_manifest_fields.py` tracks this class of bug as a ratchet, and its
list is four names shorter.
