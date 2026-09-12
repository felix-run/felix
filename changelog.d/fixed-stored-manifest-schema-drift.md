**Removing a field from the manifest schema bricked every stored manifest that set it.**
The schema is `extra=forbid`, which is what makes `spec.toolz` an error rather than a field
that silently configures nothing — but `forbid` judges authored input, and a row in
Postgres is not input. It was authored once, validated then, and has been sitting there
since. So when `spec.model.region` was removed in #125, every manifest stored before that
release stopped validating, and because the store is consulted ahead of the bundled YAML, a
stale row also shadows the file it was derived from. Found on a deployment whose `quick` was
stored two weeks before the removal: the **default manifest** answered every request with
`spec.model.region: Extra inputs are not permitted`, a perfectly good `manifests/quick.yaml`
sat there unreachable, and nothing said so until someone made a request.

Stored manifests now load through `parse_stored_manifest`, which drops fields listed in
`felix.manifests.compat.RETIRED` and warns, naming the manifest and the field so an
operator knows to re-save it. Authoring is untouched — a PUT or a YAML file carrying a
retired field still fails, because its author can fix it and should be told to — and a typo
still fails on both paths, since the list is explicit rather than blanket tolerance. Adding
to `RETIRED` is now the price of removing a field, and only for a removal that is inert; one
that changes how an agent compiles still needs a migration that rewrites the rows.
