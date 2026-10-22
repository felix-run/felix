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
        # Scheduled work: what refuses `schedule_task` to it and to everything it starts.
        scheduled=True,
    )


def test_a_scope_string_is_not_read_as_its_letters() -> None:
    auth = _job_auth(make_settings(), "acme", _job("alice", {"scopes": "admin", "scheme": "access"}))
    assert auth.scopes == frozenset()


@pytest.mark.parametrize(
    ("created_by", "created_auth"), [(None, None), ("alice", None), (None, {"scopes": ["x"]})]
)
def test_a_job_with_no_recorded_writer_runs_holding_nothing(created_by: Any, created_auth: Any) -> None:
    assert _job_auth(make_settings(), "acme", _job(created_by, created_auth)) == AuthContext(
        tenant_id="acme", principal_sub="cron", anonymous=False, scheduled=True
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


# --- a writer who signed in through Felix ---------------------------------------------------------
#
# A sign-in token has no record beyond its claims, so a job its writer saved used to replay the
# recorded grant for ever: removing a person's Google entry, or an org's grant, stopped their next
# sign-in and none of their scheduled work. The firing now asks today's configuration.


def _signin(sub: str, scopes: list[str], **claims: str) -> dict[str, Any]:
    return {"principal_sub": sub, "scopes": scopes, "anonymous": False, "scheme": "self", **claims}


def _signin_settings(**config: Any) -> Any:
    return make_settings(**{k: json.dumps(v) for k, v in config.items()})


GOOGLE = {"blake@example.com": {"tenant": "acme", "scopes": ["jobs:read"]}}
ORG = {"felix-run": {"id": 7, "tenant": "acme", "scopes": ["jobs:read", "memory:read"]}}


def test_a_google_writer_keeps_only_what_their_entry_grants_now() -> None:
    recorded = _signin("google:1", ["jobs:read", "jobs:write"], idp="google", email="Blake@Example.com")
    auth = _job_auth(_signin_settings(google_accounts=GOOGLE), "acme", _job("google:1", recorded))
    assert auth.scopes == frozenset({"jobs:read"})
    assert auth.on_behalf_of == "google:1"


@pytest.mark.parametrize(
    "accounts",
    [
        {},
        {"blake@example.com": {"tenant": "family", "scopes": ["jobs:read"]}},
        {"blake@example.com": {"tenant": "acme", "scopes": ["jobs:read"], "sub": "999"}},
    ],
    ids=["entry-removed", "moved-to-another-tenant", "pinned-to-another-account"],
)
def test_a_google_writer_the_configuration_no_longer_admits_is_refused(accounts: dict[str, Any]) -> None:
    recorded = _signin("google:1", ["jobs:read"], idp="google", email="blake@example.com")
    with pytest.raises(InboundAuthError, match="no longer grants this tenant"):
        _job_auth(_signin_settings(google_accounts=accounts), "acme", _job("google:1", recorded))


def test_a_google_writer_recorded_before_emails_were_is_found_by_their_sub_pin() -> None:
    accounts = {"blake@example.com": {"tenant": "acme", "scopes": ["jobs:read"], "sub": "1"}}
    auth = _job_auth(
        _signin_settings(google_accounts=accounts),
        "acme",
        _job("google:1", _signin("google:1", ["jobs:read"])),
    )
    assert auth.scopes == frozenset({"jobs:read"})


def test_a_github_writer_keeps_only_what_their_org_grants_now_and_is_refused_once_it_grants_nothing() -> None:
    recorded = _signin("github:42", ["jobs:read", "jobs:write"], idp="github")
    auth = _job_auth(_signin_settings(github_org_tenants=ORG), "acme", _job("github:42", recorded))
    assert auth.scopes == frozenset({"jobs:read"})
    with pytest.raises(InboundAuthError, match="no longer grants this tenant"):
        _job_auth(_signin_settings(github_org_tenants={}), "acme", _job("github:42", recorded))


def test_a_github_writer_s_personal_tenant_is_their_own_and_keeps_its_record() -> None:
    recorded = _signin("github:42", ["jobs:write"], idp="github")
    auth = _job_auth(make_settings(), "gh-42", _job("github:42", recorded))
    assert auth.scopes == frozenset({"jobs:write"})


def test_a_github_actions_writer_is_refused_once_its_repository_is_let_in_no_more() -> None:
    actions = {"repositories": {"felix": 555}, "refs": ["refs/heads/main"], "scopes": ["jobs:read"]}
    org = {"felix-run": {**ORG["felix-run"], "actions": actions}}
    recorded = _signin(
        "github-actions:555:felix-run/felix/.github/workflows/x.yml@main", ["jobs:read"], idp="github-actions"
    )
    job = _job(recorded["principal_sub"], recorded)
    assert _job_auth(_signin_settings(github_org_tenants=org), "acme", job).scopes == frozenset({"jobs:read"})
    with pytest.raises(InboundAuthError):
        _job_auth(_signin_settings(github_org_tenants=ORG), "acme", job)


def test_a_writer_whose_token_another_issuer_signed_keeps_their_record() -> None:
    """Nothing in Felix's configuration says what an outside IdP grants, so there is nothing to check."""
    recorded = {"principal_sub": "alice", "scopes": ["jobs:write"], "scheme": "access"}
    assert _job_auth(make_settings(), "acme", _job("alice", recorded)).scopes == frozenset({"jobs:write"})


def test_a_sign_in_s_idp_and_email_are_recorded_and_no_other_issuer_s_are() -> None:
    from felix.auth.recorded import record_caller

    signed_in = AuthContext(
        principal_sub="google:1",
        tenant_id="acme",
        anonymous=False,
        scheme="self",
        raw_claims={"iss": "felix-self", "idp": "google", "email": "blake@example.com"},
    )
    assert {k: record_caller(signed_in).get(k) for k in ("idp", "email")} == {
        "idp": "google",
        "email": "blake@example.com",
    }
    outsider = AuthContext(
        principal_sub="google:1",
        tenant_id="acme",
        anonymous=False,
        scheme="access",
        raw_claims={"iss": "https://idp.example", "idp": "google", "email": "x@example.com"},
    )
    assert "idp" not in record_caller(outsider) and "email" not in record_caller(outsider)
