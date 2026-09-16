"""What the compile-pin hash must and must not notice.

`pin_compile` refuses to continue a thread whose manifest changed underneath it, so the
hash is a control: too sensitive and it fires on changes that are not changes, too blunt and
it misses one that is. Both halves are asserted here, because only pinning them together
says anything -- a hash that never moves passes every drift test by being useless.

The sensitivity half is what `exclude_defaults` is for. Adding a field to `Spec` moved every
stored manifest's hash, so a thread pinned under `pin_compile` raised `ManifestDriftError`
on its next turn with nothing in its own text having changed, and every in-flight durable
fiber failed at resume -- `durability/fibers.py` forces pinning for any fiber carrying
stored auth, whatever the manifest says.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.manifests.pin import ManifestDriftError, assert_pin_matches, manifest_content_hash
from felix.manifests.schema import Manifest, Spec
from pydantic import Field

BASE: dict[str, Any] = {"pattern": "react", "tools": ["calculator"]}


def _manifest(**spec: Any) -> Manifest:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "pinned"},
            "spec": {**BASE, **spec},
        }
    )


def test_adding_a_defaulted_field_to_the_schema_does_not_move_a_hash() -> None:
    """The property this exists for, simulated the only way it can be before the fact.

    A subclass of `Spec` with one extra defaulted field is what the schema looks like after
    someone adds one. The same manifest body must hash the same through both, or the next
    schema addition repeats #254: pinned threads refusing, durable fibers failing at resume,
    and an operator with nothing to point at in their own manifest.
    """

    class SpecPlusOne(Spec):
        a_future_field: bool = False

    class ManifestPlusOne(Manifest):
        spec: SpecPlusOne = Field(default_factory=SpecPlusOne)

    body = {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "pinned"},
        "spec": BASE,
    }

    before = manifest_content_hash(Manifest.model_validate(body))
    after = manifest_content_hash(ManifestPlusOne.model_validate(body))  # type: ignore[arg-type]

    assert before == after, "a schema addition moved a manifest that did not change"


def test_writing_a_default_explicitly_hashes_the_same_as_omitting_it() -> None:
    """A field at its default is not information about the manifest: the two compile to the
    same agent, so they must pin the same. True before this change and asserted so it stays
    true -- it is the reason excluding defaults gives nothing up."""
    assert manifest_content_hash(_manifest()) == manifest_content_hash(_manifest(skills_declared_only=False))


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        ("a scalar flips", {"skills_declared_only": True}),
        ("a tool is added", {"tools": ["calculator", "list_skills"]}),
        ("a nested field moves off its default", {"governance": {"pin_compile": True}}),
        ("a float changes", {"model": {"temperature": 0.7}}),
        ("a skill is declared", {"skills": [{"name": "calculator-help"}]}),
        ("the pattern changes", {"pattern": "plan_execute"}),
        # The security-relevant ref lists, because recursion *into a list of models* is
        # where excluding defaults is most plausibly wrong: an item whose own fields sit at
        # their defaults collapses, and the question is whether the item itself survives.
        # `mcp_servers` also exercises the alias, since `Spec.mcp` dumps under it.
        (
            "an MCP server is bound",
            {"mcp_servers": [{"name": "docs", "url": "https://example.com/mcp"}]},
        ),
        (
            "an MCP server's url moves",
            {"mcp_servers": [{"name": "docs", "url": "https://elsewhere.test/mcp"}]},
        ),
        ("a peer is bound", {"peers": [{"name": "billing", "url": "https://peer.test/a2a"}]}),
        (
            "a policy's required scopes change",
            {"policies": [{"id": "calc", "tools": ["calculator"], "required_scopes": ["tools:calc"]}]},
        ),
        ("inbound scopes are required", {"auth": {"inbound": {"required_scopes": ["chat:write"]}}}),
        ("a limit is tightened", {"limits": {"max_tool_calls": 3}}),
    ],
)
def test_a_real_change_still_moves_the_hash(label: str, changed: dict[str, Any]) -> None:
    """The other half. Excluding defaults must not blind the control it serves."""
    assert manifest_content_hash(_manifest()) != manifest_content_hash(_manifest(**changed)), label


def test_reverting_a_field_to_its_default_moves_the_hash_too() -> None:
    """The direction excluding defaults could plausibly have broken: turning a setting off
    removes a key rather than changing one, and a hash that noticed only additions would let
    a pinned thread keep running after its governance was switched off."""
    on = _manifest(governance={"pin_compile": True})
    off = _manifest()

    assert manifest_content_hash(on) != manifest_content_hash(off)


def test_the_pin_check_refuses_on_a_real_drift() -> None:
    """End to end through `assert_pin_matches`, because the hash is only a control if the
    thing that reads it refuses."""
    pinned = {"pin_compile": True, "manifest_hash": manifest_content_hash(_manifest())}

    assert_pin_matches(pinned, _manifest())  # unchanged: no raise

    with pytest.raises(ManifestDriftError):
        assert_pin_matches(pinned, _manifest(tools=["calculator", "list_skills"]))
