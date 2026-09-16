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


def assert_pin_matches(
    pinned: dict[str, Any] | None,
    manifest: Manifest,
    *,
    version: int | None = None,
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
    pinned_version = pinned.get("manifest_version")
    if pinned_version is not None and version is not None and int(pinned_version) != int(version):
        raise ManifestDriftError(
            f"manifest version drift for {manifest.metadata.name}: pinned={pinned_version} current={version}"
        )


async def ensure_thread_pin(
    *,
    settings: Any,
    tenant_id: str,
    thread_id: str | None,
    manifest: Manifest,
    version: int | None = None,
) -> dict[str, Any]:
    """Check drift against prior pin; store pin when ``pin_compile`` is enabled."""
    from felix.session.thread_state import get_thread_meta, update_thread_meta

    fields = pin_fields(manifest, version=version)
    if not thread_id:
        return fields

    meta = await get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread_id)
    pinned = {
        "manifest_name": meta.get("manifest_name"),
        "manifest_version": meta.get("manifest_version"),
        "manifest_hash": meta.get("manifest_hash"),
        "pin_compile": meta.get("pin_compile"),
    }
    if pinned.get("manifest_hash"):
        assert_pin_matches(pinned, manifest, version=version)

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
    "ensure_thread_pin",
    "manifest_content_hash",
    "pin_fields",
]
