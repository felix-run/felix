"""Manifest compile pin — content hash + drift checks for pin_compile."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from felix.manifests.schema import Manifest


class ManifestDriftError(ValueError):
    """Pinned compile hash no longer matches the active manifest."""


def manifest_content_hash(manifest: Manifest) -> str:
    """Stable SHA-256 of the canonical manifest JSON (refs, not resolved secrets).

    `exclude_defaults` is what makes "stable" true across releases rather than only within
    one. Without it the dump carries every field the schema declares, so *adding* a field to
    `Spec` -- with a default, touching nothing -- moves the hash of every manifest already
    stored. That is not theoretical: `spec.skills_declared_only` did it in #254, and the
    blast radius is a thread pinned under `pin_compile` raising `ManifestDriftError` on its
    next turn with nothing in its manifest having changed, plus every in-flight durable
    fiber failing at resume, since `durability/fibers.py` forces pinning for any fiber
    carrying stored auth. Fail-closed, and still an outage an operator did not ask for.

    Almost nothing is given up, and the exception is worth stating rather than glossing. A
    field at its default is not usually information about the manifest -- one that writes
    `pin_compile: false` and one that omits it compile to the same agent, and the old dump
    already hashed those two identically -- so for a *manifest* edit this changes nothing.
    Drift detection is untouched in both directions: moving a field off its default adds a
    key and moving it back removes one, which matters because a hash that noticed only
    additions would let a pinned thread keep running after its governance was switched off.

    What it does give up is a *release* that changes what a default means. Under the old
    hash, shipping a new default for, say, `content_screening.on_flag` moved every stored
    manifest that omitted the field, and a pin fired; now it does not, and those manifests
    compile differently while hashing the same. **Changing a default is therefore a
    migration** -- rewrite the rows or rotate the pins -- and it joins the family
    `manifests/compat.py` already names, alongside removing a key and narrowing a field.
    A second digest over `Spec()`'s own defaults would catch it and would also move on every
    field addition, which is the outage this exists to remove; so it is documented instead.

    One field also breaks the "written default == omitted" rule today, independently of
    this: `session.context_window_tokens` is read through `model_fields_set` in
    `runtime.py`, so writing its default and omitting it mean different things there and
    hash the same. The old hash was equally blind to it. `docs/ROADMAP.md` carries the fix,
    which is to make the schema default a sentinel so the serialized form matches the
    meaning.

    Changing this rotates every hash exactly once, which is the same one-time cost as the
    additions it prevents -- taken deliberately here rather than accidentally on the next
    schema change. `tests/unit/test_manifest_pin_hash.py` pins the properties above.
    """
    payload = manifest.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def sub_agents_hash(
    settings: Any,
    tenant_id: str,
    manifest: Manifest,
    *,
    _path: tuple[str, ...] = (),
    _seen: dict[str, list[Any]] | None = None,
    resolved_out: dict[str, Manifest | None] | None = None,
) -> str | None:
    """A digest of every sub-agent this manifest compiles, as the tenant resolves them now.

    `manifest_content_hash` covers the parent alone, which was a complete pin while children
    came from bundled YAML and changed only with a deploy. Since children resolve through the
    tenant's store (#317), an edited child — its tools, policies, approvals — reached a thread
    pinned under `pin_compile` on its next turn without a word. This covers the tree: each
    child's name and content hash, and its own children, the way `build_agent` walks them.

    `None` for a manifest with no sub-agents, so its pin is exactly what it was. A child that
    resolves nowhere, or a cycle, is recorded as such rather than raising: the compile refuses
    both, and a pin check is not the place to report them.

    Each child is resolved and hashed once per call, however many parents name it — the memo
    `build_agent` keeps in `BuildDeps.compiled`. Without it a diamond (twenty children all
    naming the same twenty) cost fan-out to the power of depth per turn while the compile
    stayed linear, on a process other tenants share.
    """
    names = list(dict.fromkeys(manifest.spec.sub_agents))
    if not names:
        return None
    from felix.manifests.builder import MAX_SUB_AGENT_DEPTH
    from felix.runtime import resolve_tenant_manifest

    path = (*_path, manifest.metadata.name)
    seen: dict[str, list[Any]] = {} if _seen is None else _seen
    parts: list[list[Any]] = []
    for name in names:
        if name in seen:
            parts.append(seen[name])
            continue
        if name in path or len(path) > MAX_SUB_AGENT_DEPTH:
            parts.append([name, "unresolvable"])
            continue
        try:
            child = (await resolve_tenant_manifest(settings, tenant_id, name)).manifest
        except LookupError, ValueError:
            parts.append([name, None])
            if resolved_out is not None:
                resolved_out[name] = None
            continue
        if resolved_out is not None:
            resolved_out[name] = child
        seen[name] = [
            name,
            manifest_content_hash(child),
            await sub_agents_hash(
                settings, tenant_id, child, _path=path, _seen=seen, resolved_out=resolved_out
            ),
        ]
        parts.append(seen[name])
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pin_fields(
    manifest: Manifest,
    *,
    version: int | None = None,
) -> dict[str, Any]:
    return {
        "manifest_name": manifest.metadata.name,
        "manifest_version": version,
        "manifest_hash": manifest_content_hash(manifest),
        "pin_compile": bool(manifest.spec.governance.pin_compile),
    }


async def pin_fields_for(
    settings: Any,
    tenant_id: str,
    manifest: Manifest,
    *,
    version: int | None = None,
    resolved_out: dict[str, Manifest | None] | None = None,
) -> dict[str, Any]:
    """`pin_fields` plus the sub-agent digest — what a pin records when it will be enforced."""
    fields = pin_fields(manifest, version=version)
    fields["sub_agents_hash"] = await sub_agents_hash(
        settings, tenant_id, manifest, resolved_out=resolved_out
    )
    return fields


def assert_pin_matches(
    pinned: dict[str, Any] | None,
    manifest: Manifest,
    *,
    version: int | None = None,
    sub_agents: str | None = None,
) -> None:
    """Refuse continue/resume when ``pin_compile`` and the hash drifted."""
    if not pinned:
        return
    if not pinned.get("pin_compile") and not manifest.spec.governance.pin_compile:
        return
    expected = pinned.get("manifest_hash")
    if not expected:
        return
    actual = manifest_content_hash(manifest)
    if expected != actual:
        raise ManifestDriftError(
            f"manifest hash drift for {manifest.metadata.name}: "
            f"pinned={expected[:12]}… current={actual[:12]}…"
        )
    # Checked only when the pin recorded it: a pin taken before sub-agents were covered has no
    # digest to compare, and treating that as drift would fail every such thread at once.
    # `ensure_thread_pin` records it on that thread's next turn.
    expected_children = pinned.get("sub_agents_hash")
    if expected_children and expected_children != sub_agents:
        raise ManifestDriftError(
            f"sub-agent drift for {manifest.metadata.name}: a sub-agent it compiles was edited, "
            "added or removed since this thread was pinned"
        )
    pinned_version = pinned.get("manifest_version")
    if pinned_version is not None and version is not None and int(pinned_version) != int(version):
        raise ManifestDriftError(
            f"manifest version drift for {manifest.metadata.name}: pinned={pinned_version} current={version}"
        )


async def assert_resume_pin(
    settings: Any,
    tenant_id: str,
    pinned: dict[str, Any] | None,
    manifest: Manifest,
    *,
    version: int | None = None,
    resolved_out: dict[str, Manifest | None] | None = None,
) -> None:
    """`assert_pin_matches` for a durable run resuming, with its sub-agents re-resolved.

    The children are resolved only when the pin recorded a digest — which every pin taken at
    enqueue now does for a manifest with sub-agents — so a fiber enqueued before this carries
    on as it would have, and a single-agent fiber pays nothing.
    """
    children = None
    if pinned and pinned.get("sub_agents_hash"):
        children = await sub_agents_hash(settings, tenant_id, manifest, resolved_out=resolved_out)
    assert_pin_matches(pinned, manifest, version=version, sub_agents=children)


async def ensure_thread_pin(
    *,
    settings: Any,
    tenant_id: str,
    thread_id: str | None,
    manifest: Manifest,
    version: int | None = None,
    resolved_out: dict[str, Manifest | None] | None = None,
) -> dict[str, Any]:
    """Check drift against prior pin; store pin when ``pin_compile`` is enabled."""
    from felix.session.thread_state import get_thread_meta, update_thread_meta

    fields = pin_fields(manifest, version=version)
    if not thread_id:
        return fields
    if fields["pin_compile"]:
        # Resolving the children costs a store read each; only an enforced pin pays it.
        fields = await pin_fields_for(
            settings, tenant_id, manifest, version=version, resolved_out=resolved_out
        )

    meta = await get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread_id)
    pinned = {
        "manifest_name": meta.get("manifest_name"),
        "manifest_version": meta.get("manifest_version"),
        "manifest_hash": meta.get("manifest_hash"),
        "pin_compile": meta.get("pin_compile"),
        "sub_agents_hash": meta.get("sub_agents_hash"),
    }
    if pinned.get("manifest_hash"):
        assert_pin_matches(pinned, manifest, version=version, sub_agents=fields.get("sub_agents_hash"))

    if fields["pin_compile"] or not pinned.get("manifest_hash"):
        # Always record hash on first touch; enforce only when pin_compile.
        if fields["pin_compile"]:
            await update_thread_meta(
                settings=settings,
                tenant_id=tenant_id,
                thread_id=thread_id,
                **fields,
            )
        elif not pinned.get("manifest_hash"):
            # Soft record without enforcement for observability.
            await update_thread_meta(
                settings=settings,
                tenant_id=tenant_id,
                thread_id=thread_id,
                manifest_name=fields["manifest_name"],
                manifest_version=fields["manifest_version"],
                manifest_hash=fields["manifest_hash"],
                pin_compile=False,
            )
    return fields


__all__ = [
    "ManifestDriftError",
    "assert_pin_matches",
    "assert_resume_pin",
    "ensure_thread_pin",
    "manifest_content_hash",
    "pin_fields",
    "pin_fields_for",
    "sub_agents_hash",
]
