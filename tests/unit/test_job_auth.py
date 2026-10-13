"""Who a scheduled job's firing runs as, read off the job row (`jobs.scheduler._job_auth`).

The whole `AuthContext`, compared field for field: a firing that dropped the pin, kept the
writer's personal library, or turned a stored string into five one-letter scopes would each pass
a test that looked at one field.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.context import AuthContext
from felix.jobs.scheduler import _job_auth
from felix.manifests.inbound_auth import InboundAuthError

from tests.support.factories import make_settings


def _job(created_by: str | None, created_auth: Any) -> dict[str, Any]:
    return {"name": "nightly", "created_by": created_by, "created_auth": created_auth}


def test_a_jwt_writer_s_job_runs_as_cron_for_them_with_their_recorded_grant() -> None:
    recorded = {
        "principal_sub": "alice",
        "scopes": ["ops:run", "jobs:write"],
        "scheme": "access",
        "anonymous": False,
        "felix_manifests": ["quick"],
        "recorded_at": 1,
    }
    assert _job_auth(make_settings(), "acme", _job("alice", recorded)) == AuthContext(
        tenant_id="acme",
        principal_sub="cron",
        on_behalf_of="alice",
        scopes=frozenset({"ops:run", "jobs:write"}),
        anonymous=False,
        scheme="access",
        raw_claims={"felix_manifests": ["quick"]},
        skill_owner=None,
    )


def test_a_scope_string_is_not_read_as_its_letters() -> None:
    auth = _job_auth(make_settings(), "acme", _job("alice", {"scopes": "admin", "scheme": "access"}))
    assert auth.scopes == frozenset()


@pytest.mark.parametrize(
    ("created_by", "created_auth"), [(None, None), ("alice", None), (None, {"scopes": ["x"]})]
)
def test_a_job_with_no_recorded_writer_runs_holding_nothing(created_by: Any, created_auth: Any) -> None:
    assert _job_auth(make_settings(), "acme", _job(created_by, created_auth)) == AuthContext(
        tenant_id="acme", principal_sub="cron", anonymous=False
    )


def _api_key_settings(entries: dict[str, dict[str, Any]]) -> Any:
    return make_settings(auth_mode="api_key", auth_api_keys=json.dumps(entries))


def test_an_api_key_writer_keeps_only_what_their_key_holds_now() -> None:
    settings = _api_key_settings(
        {
            "k-one": {"tenant_id": "acme", "sub": "alice", "scopes": ["jobs:write"]},
            "k-two": {"tenant_id": "acme", "sub": "alice", "scopes": ["b:read"]},
            # Same subject, another tenant: grants nothing here.
            "k-three": {"tenant_id": "other", "sub": "alice", "scopes": ["ops:run"]},
        }
    )
    recorded = {"scopes": ["jobs:write", "ops:run", "b:read"], "scheme": "api_key"}
    auth = _job_auth(settings, "acme", _job("alice", recorded))
    assert auth.scopes == frozenset({"jobs:write", "b:read"})
    assert auth.acting_for == "alice"


@pytest.mark.parametrize(
    "settings",
    [
        _api_key_settings({"k-one": {"tenant_id": "acme", "sub": "bob", "scopes": ["jobs:write"]}}),
        make_settings(auth_mode="jwt"),
    ],
    ids=["key-removed", "no-longer-api-key-mode"],
)
def test_an_api_key_writer_whose_key_is_gone_is_refused(settings: Any) -> None:
    with pytest.raises(InboundAuthError, match="no longer configured"):
        _job_auth(settings, "acme", _job("alice", {"scopes": ["jobs:write"], "scheme": "api_key"}))
