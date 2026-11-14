"""The bundled coding workspace must remain usable across approvals and recovery."""

from felix.durability.fibers import FIBER_LEASE_MS
from felix.manifests.loader import load_bundled


def test_code_authority_outlives_its_work_and_recovery_budget() -> None:
    spec = load_bundled("code").spec
    ttl = spec.execution.resume_token_ttl_seconds
    assert ttl is not None
    assert ttl > spec.limits.max_wall_clock_seconds + FIBER_LEASE_MS // 1000
    assert ttl > max(rule.ttl_seconds for rule in spec.approvals)


def test_code_gates_every_mutating_client_tool() -> None:
    spec = load_bundled("code").spec
    gated = {name for rule in spec.approvals for name in rule.tools}
    assert {"local_write", "local_edit", "local_delete", "local_rename", "local_shell"} <= gated
    assert not spec.auth.inbound.allow_anonymous
    assert spec.content_screening.enabled
