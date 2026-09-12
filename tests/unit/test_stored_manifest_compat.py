"""A field removed from the schema must not brick manifests already in a store.

`spec.model.region` went in #125, whose reasoning was "a manifest setting it fails
validation rather than carrying a field that configures nothing". That is right for
someone authoring a manifest today and fatal for one stored in August: the schema is
`extra=forbid`, so every stored manifest that set the field stopped validating. Because
`resolve_tenant_manifest` reads the store before the bundled YAML, a stale row also
shadows a perfectly good `manifests/quick.yaml` — so the *default* manifest answered every
request with `spec.model.region: Extra inputs are not permitted`, and nothing said so until
someone made a request.

Found on a real deployment, in a database whose `quick` was stored two weeks before the
removal. These tests pin both halves: a retired field loads from a store, and absolutely
nothing else gets that latitude.

Several of them inject into `RETIRED` rather than relying on its contents. That is
deliberate — it holds one entry today, and a suite that only ever exercises one entry
cannot see the bugs that appear with the second, which is the entry someone adds the next
time they remove a field.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
from felix.manifests import compat
from felix.manifests.compat import RETIRED, drop_retired
from felix.manifests.loader import ManifestParseError, parse_manifest, parse_stored_manifest

ORIGIN = "default/quick v2"


def _body(**model_extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "quick"},
        "spec": {"pattern": "react", "model": {"temperature": 0.0, **model_extra}},
    }


def test_a_stored_manifest_with_a_retired_field_still_loads() -> None:
    """The regression, in the shape it actually occurred: `quick`, stored, with `region`."""
    manifest = parse_stored_manifest(_body(region=None), origin=ORIGIN)
    assert manifest.metadata.name == "quick"
    assert manifest.spec.model.temperature == 0.0


def test_the_same_manifest_is_still_refused_when_authored() -> None:
    """`extra=forbid` is why `spec.toolz` is an error instead of a no-op, and the removal
    of `region` was deliberate. Neither is walked back — a PUT or a YAML file carrying a
    retired field still fails, because its author can fix it and should be told to.
    """
    with pytest.raises(ManifestParseError) as exc:
        parse_manifest(_body(region=None))
    assert "region" in str(exc.value)


def test_a_typo_is_not_a_retired_field_and_the_row_is_named() -> None:
    """The whole value of the list is that it is a list.

    Blanket tolerance on the read path would mean a stored manifest naming `spec.modle`
    loads and silently configures nothing — trading a loud failure for the exact defect
    `extra=forbid` exists to prevent.

    The message has to name the row, too. A bare `spec.modle: Extra inputs are not
    permitted` says nothing about *which* stored manifest is unserviceable, and "which
    one" is the operator's next question — that anonymity is a good part of why the
    original outage went unnoticed.
    """
    with pytest.raises(ManifestParseError) as exc:
        parse_stored_manifest(_body(regionn=None), origin=ORIGIN)
    assert "regionn" in str(exc.value)
    assert ORIGIN in str(exc.value)


def test_a_retired_field_is_dropped_rather_than_defaulted() -> None:
    """It is removed from the body, not passed through as None to a field that is gone.

    The `dropped` list is an internal report, coupled to `log_dropped` which renders it;
    asserted here because that warning is the only thing that tells an operator to act.
    """
    cleaned, dropped = drop_retired(_body(region="us-east-1"))
    assert dropped == [("spec", "model", "region")]
    assert "region" not in cleaned["spec"]["model"]
    assert cleaned["spec"]["model"]["temperature"] == 0.0


def test_dropping_never_mutates_the_caller_s_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolver caches parsed manifests and raw bodies are shared.

    Editing in place would reach through a cache into whatever else holds the object —
    and the corruption would surface somewhere with no connection to this function.

    Two entries on *sibling* subtrees, because one cannot see the bug this guards. Copying
    once on first write leaves every subtree the copy did not touch shared with the
    caller, so the second drop pops straight out of the caller's dict. The first version
    of `drop_retired` did that, and a single-entry `RETIRED` hid it completely.
    """
    monkeypatch.setitem(RETIRED, ("spec", "session", "gone"), "test-only")
    body = _body(region="us-east-1")
    body["spec"]["session"] = {"gone": 1, "strategy": "full_replay"}
    before = copy.deepcopy(body)

    cleaned, dropped = drop_retired(body)

    assert body == before, "drop_retired reached through into the caller's body"
    assert sorted(dropped) == [("spec", "model", "region"), ("spec", "session", "gone")]
    assert "region" not in cleaned["spec"]["model"]
    assert "gone" not in cleaned["spec"]["session"]
    assert cleaned["spec"]["session"]["strategy"] == "full_replay"


def test_a_body_with_nothing_retired_is_returned_unchanged() -> None:
    """Identity, not equality, on purpose: this is the only expression of "the common path
    does not rebuild every manifest on every read". A refactor that always shallow-copies
    turns this red — that is the point, not a reason to weaken it to `==`.
    """
    body = _body()
    cleaned, dropped = drop_retired(body)
    assert cleaned is body
    assert dropped == []


@pytest.mark.parametrize("raw", [None, [], "not-a-mapping", 7])
def test_a_body_that_is_not_a_mapping_is_left_for_the_validator(raw: Any) -> None:
    """Garbage gets the schema's error, not an AttributeError from this shim."""
    cleaned, dropped = drop_retired(raw)
    assert cleaned is raw and dropped == []
    with pytest.raises(ManifestParseError):
        parse_stored_manifest(raw, origin=ORIGIN)


def test_a_partial_path_does_not_crash_or_drop() -> None:
    """`spec.model` absent, or not a mapping, is a manifest the validator should judge."""
    for model in (None, "sonnet", []):
        body = {"apiVersion": "felix/v1", "kind": "Agent", "spec": {"model": model}}
        cleaned, dropped = drop_retired(body)
        assert dropped == [] and cleaned is body


def test_a_stored_manifest_that_is_invalid_for_any_other_reason_still_fails() -> None:
    """Only a removed *key* is rescued, and that limit is a decision worth recording.

    A narrowed `Literal`, a new required field or a new validator bricks a stored row
    exactly as before — including the shadowing, since the store is still read ahead of
    the bundled file. `spec.memory.checkpointer` is the live example: it went from a
    `Literal` to a registry lookup in #109, so a stored `agentcore` parses here and then
    raises deep in `build_tenant_agent`. That needs its own mechanism; this one must not
    be stretched to cover it silently.
    """
    body = _body()
    body["spec"]["pattern"] = 17  # wrong type, not an extra key
    with pytest.raises(ManifestParseError) as exc:
        parse_stored_manifest(body, origin=ORIGIN)
    assert "pattern" in str(exc.value)


def _fields_by_accepted_name(model: Any) -> dict[str, Any]:
    """Field names *and* aliases, which is what a stored body may legally use.

    `Spec.mcp` is aliased `mcp_servers` with `populate_by_name`, so a manifest may hold
    either spelling. Checking only `model_fields` would let a `RETIRED` entry written in
    the alias spelling pass while the field is live — the silent-strip the test below
    exists to prevent, reached by the other name.
    """
    fields = dict(getattr(model, "model_fields", {}))
    for field in list(fields.values()):
        alias = getattr(field, "alias", None)
        if alias:
            fields[alias] = field
    return fields


def test_every_retired_path_is_absent_from_the_current_schema() -> None:
    """A stale entry here would silently drop a field that works.

    This is the failure mode the list itself could introduce: leave `spec.model.region` in
    place after someone re-adds a `region` field, and every stored manifest setting it is
    quietly stripped on load — a control that looks present and does nothing, arriving
    through the very mechanism added to prevent one.

    Two floors under the scan, because without them it passes by not running. An empty
    `RETIRED` makes the loop body unreachable, and a path whose parent is a `list[Model]`
    or an `X | None` walks off the end of the models and compares against `{}` — which is
    also exactly where `drop_retired` silently matches nothing, so such an entry would be
    approved here *and* do nothing there.
    """
    from felix.manifests.schema import Manifest
    from pydantic import BaseModel

    assert RETIRED, "nothing to check — an empty registry makes this test vacuous"

    for path in RETIRED:
        model: Any = Manifest
        for part in path[:-1]:
            fields = _fields_by_accepted_name(model)
            assert part in fields, f"{'.'.join(path)}: `{part}` is not a field of {model.__name__}"
            model = fields[part].annotation
            assert isinstance(model, type) and issubclass(model, BaseModel), (
                f"{'.'.join(path)}: `{part}` is {model!r}, which this list cannot descend into "
                "(a list item or an optional) — the entry would match nothing on load"
            )
        assert path[-1] not in _fields_by_accepted_name(model), (
            f"{'.'.join(path)} is listed as retired but the schema has it again — "
            "stored manifests setting it are being silently stripped"
        )


@pytest.mark.asyncio
async def test_the_resolver_serves_a_stored_row_written_before_the_removal() -> None:
    """The production path, not a convenient one: a row in the store, resolved by name.

    The row goes in through `put_version` — which also sets the active pointer — and only
    the retired key is injected afterwards, because `put_version` takes an already-validated
    `Manifest` and so cannot express the thing under test. Hand-rolling the row dicts
    instead would make this ERROR rather than fail the day a column is added, and an ERROR
    is not evidence.

    Without the compat read this raises `ManifestParseError` and the request 400s.
    """
    from felix.config import Settings
    from felix.manifests import store as manifest_store
    from felix.runtime import resolve_tenant_manifest

    settings = Settings(database_url="memory://compat", object_store="memory")
    body = _body()
    body["metadata"]["name"] = "legacy"
    row = await manifest_store.put_version(settings, "default", "legacy", parse_manifest(body))

    stored = manifest_store._memory_manifests[("default", "legacy", row["version"])]
    stored["manifest_json"]["spec"]["model"]["region"] = None

    resolved = await resolve_tenant_manifest(settings, "default", "legacy")
    assert resolved.manifest.metadata.name == "legacy"
    assert resolved.source == "tenant_postgres"
    assert resolved.version == row["version"]


def test_the_operator_is_told_once_rather_than_quietly_accommodated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A manifest rewritten on every read with nobody told is the same class of bug.

    One record, not one per field or one per read — and it names the manifest and the
    field, so the fix is obvious from the log line. It says *delete*, not merely re-save:
    `GET /manifests/{name}?version=N` echoes the stored body with the retired field still
    in it and `PUT` validates strictly, so a plain read-modify-write round trip is refused.
    """
    with caplog.at_level(logging.WARNING, logger=compat.logger.name):
        parse_stored_manifest(_body(region=None), origin=ORIGIN)

    records = [r for r in caplog.records if r.name == compat.logger.name]
    assert len(records) == 1, f"expected exactly one warning, got {[r.getMessage() for r in records]}"
    message = records[0].getMessage()
    assert ORIGIN in message
    assert "spec.model.region" in message
    assert "Delete" in message


def test_nothing_is_logged_when_nothing_was_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Every clean manifest read would otherwise carry a warning nobody can act on."""
    with caplog.at_level(logging.WARNING, logger=compat.logger.name):
        parse_stored_manifest(_body(), origin=ORIGIN)
    assert [r for r in caplog.records if r.name == compat.logger.name] == []


def test_a_tenant_id_cannot_forge_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """`origin` carries a tenant id, and a log line's separator is the newline.

    `assert_valid_tenant_id` rejects `:` and `#` — the delimiters it cares about — and
    nothing else, so `acme\\nWARNING  all clear` is an *accepted* tenant id. Interpolated
    raw it ends the record and starts a second one that reads as though the harness said
    it. Reproduced before this was fixed; CodeQL flagged the same line independently.

    One record, and the newline visible in it rather than acted on.
    """
    hostile = "acme\nWARNING  forged: all clear/quick v2"
    with caplog.at_level(logging.WARNING, logger=compat.logger.name):
        parse_stored_manifest(_body(region=None), origin=hostile)

    records = [r for r in caplog.records if r.name == compat.logger.name]
    assert len(records) == 1, "the log line was split in two"
    message = records[0].getMessage()
    assert "\n" not in message
    assert "\\n" in message, "the newline should be shown, not silently stripped"


def test_the_parse_failure_message_is_one_line_too() -> None:
    """The same value reaches an exception that is logged and returned over HTTP."""
    hostile = "acme\nWARNING  forged/quick v2"
    with pytest.raises(ManifestParseError) as exc:
        parse_stored_manifest(_body(regionn=None), origin=hostile)
    assert "\n" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain/quick v2", "plain/quick v2"),
        ("a\nb", "a\\nb"),
        ("a\rb", "a\\rb"),
        ("a\tb", "a\\tb"),
        ("a\u2028b", "a\\u2028b"),  # a line separator a naive \n filter misses
        ("a\x00b", "a\\x00b"),
    ],
)
def test_one_line_escapes_every_shape_of_line_break(raw: str, expected: str) -> None:
    assert compat.one_line(raw) == expected


def test_one_line_bounds_an_attacker_influenced_string() -> None:
    """A log line is not a place to put an unbounded value."""
    assert len(compat.one_line("x" * 5000)) < 250


def test_a_multi_line_retirement_reason_cannot_split_the_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reasons in `RETIRED` are hand-written source strings, not manifest data.

    That makes this the self-inflicted half of the same bug: nothing stops the next reason
    being wrapped across two lines, and a newline there splits the record exactly as a
    hostile tenant id would. CodeQL flagged the argument for its own reason — `drop_retired`
    returns `(cleaned, dropped)`, so taint analysis treats the whole tuple as manifest-
    derived — and the honest answer to both is the same one rule: everything interpolated
    into a log record goes through `one_line`.
    """
    monkeypatch.setitem(RETIRED, ("spec", "session", "gone"), "removed in 9.9.9\nWARNING  forged: all clear")
    body = _body()
    body["spec"]["session"] = {"gone": 1}

    with caplog.at_level(logging.WARNING, logger=compat.logger.name):
        parse_stored_manifest(body, origin=ORIGIN)

    records = [r for r in caplog.records if r.name == compat.logger.name]
    assert len(records) == 1, "a reason string split the log record in two"
    assert "\n" not in records[0].getMessage()


def test_every_retired_field_is_named_however_many_there_are(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Escaping per entry rather than after the join, because `one_line` truncates.

    Capping the joined string would drop the tail of the field list — the actionable half
    of the message, and the half an operator needs to know what to delete.
    """
    for i in range(6):
        monkeypatch.setitem(RETIRED, ("spec", "session", f"gone{i}"), "x" * 60)
    body = _body()
    body["spec"]["session"] = {f"gone{i}": 1 for i in range(6)}

    with caplog.at_level(logging.WARNING, logger=compat.logger.name):
        parse_stored_manifest(body, origin=ORIGIN)

    message = next(r for r in caplog.records if r.name == compat.logger.name).getMessage()
    for i in range(6):
        assert f"spec.session.gone{i}" in message, f"entry {i} was truncated out of the warning"
