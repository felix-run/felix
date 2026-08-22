"""Canary hash bucketing — deterministic stable/canary selection."""

from __future__ import annotations

from felix.manifests.resolver import pick_variant


def test_weight_zero_always_stable() -> None:
    assert (
        pick_variant(
            tenant_id="acme",
            thread_id="acme:t1",
            manifest_name="x",
            stable_version=1,
            canary_version=2,
            canary_weight=0,
        )
        == "stable"
    )


def test_weight_hundred_always_canary() -> None:
    assert (
        pick_variant(
            tenant_id="acme",
            thread_id="acme:t1",
            manifest_name="x",
            stable_version=1,
            canary_version=2,
            canary_weight=100,
        )
        == "canary"
    )


def test_empty_thread_always_stable() -> None:
    assert (
        pick_variant(
            tenant_id="acme",
            thread_id="",
            manifest_name="x",
            stable_version=1,
            canary_version=2,
            canary_weight=50,
        )
        == "stable"
    )


def test_deterministic_per_thread() -> None:
    a = pick_variant(
        tenant_id="acme",
        thread_id="acme:thr-1",
        manifest_name="x",
        stable_version=1,
        canary_version=2,
        canary_weight=50,
    )
    b = pick_variant(
        tenant_id="acme",
        thread_id="acme:thr-1",
        manifest_name="x",
        stable_version=1,
        canary_version=2,
        canary_weight=50,
    )
    assert a == b


def test_version_flip_rebuckets_key() -> None:
    a = pick_variant(
        tenant_id="acme",
        thread_id="acme:thr-1",
        manifest_name="x",
        stable_version=1,
        canary_version=2,
        canary_weight=50,
    )
    b = pick_variant(
        tenant_id="acme",
        thread_id="acme:thr-1",
        manifest_name="x",
        stable_version=1,
        canary_version=3,
        canary_weight=50,
    )
    # Same thread may land differently after a version flip — both must be valid.
    assert a in {"stable", "canary"}
    assert b in {"stable", "canary"}
