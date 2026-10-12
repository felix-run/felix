"""A credential's `manifests` claim: the manifests it may invoke, and nothing else.

`enforce_inbound_auth` is the one function every invoke surface calls (chat, /v1, A2A, MCP,
side questions, delegation), so this is where the pin either holds everywhere or nowhere. The
e2e half — a real Google sign-in reaching `kids` and refused at `quick` — is
`tests/e2e/test_google_login.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.auth.context import MANIFESTS_CLAIM, AuthContext, Principal, _empty_outbound
from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
from felix.manifests.loader import load_manifest_file

MANIFESTS = Path(__file__).resolve().parents[2] / "manifests"


def _auth(claims: dict[str, Any]) -> AuthContext:
    return AuthContext(
        principal=Principal(subject="google:1", tenant_id="kids", scheme="self"),
        outbound_token=_empty_outbound,
        raw_claims={"sub": "google:1", **claims},
    )


@pytest.fixture(scope="module")
def kids() -> Any:
    return load_manifest_file(MANIFESTS / "kids.yaml")


@pytest.fixture(scope="module")
def quick() -> Any:
    return load_manifest_file(MANIFESTS / "quick.yaml")


def test_a_pinned_credential_reaches_its_manifest(kids: Any) -> None:
    enforce_inbound_auth(kids, _auth({MANIFESTS_CLAIM: ["kids"]}))


def test_a_pinned_credential_is_refused_everywhere_else(quick: Any) -> None:
    with pytest.raises(InboundAuthError) as refused:
        enforce_inbound_auth(quick, _auth({MANIFESTS_CLAIM: ["kids"]}))
    assert refused.value.status_code == 403
    assert "'quick'" in refused.value.detail


@pytest.mark.parametrize("claim", ["kids", None, {"kids": True}, [1], [["kids"]]], ids=repr)
def test_a_malformed_pin_is_refused_not_ignored(kids: Any, claim: Any) -> None:
    """Ignoring a pin it cannot read would turn a malformed pin into no pin at all."""
    with pytest.raises(InboundAuthError, match="malformed manifest pin"):
        enforce_inbound_auth(kids, _auth({MANIFESTS_CLAIM: claim}))


def test_an_empty_pin_reaches_nothing(kids: Any) -> None:
    with pytest.raises(InboundAuthError):
        enforce_inbound_auth(kids, _auth({MANIFESTS_CLAIM: []}))


def test_no_claim_is_no_pin(quick: Any, kids: Any) -> None:
    enforce_inbound_auth(quick, _auth({}))
    enforce_inbound_auth(kids, _auth({}))


def test_the_kids_manifest_refuses_an_anonymous_caller(kids: Any) -> None:
    from felix.auth.context import ANONYMOUS

    with pytest.raises(InboundAuthError) as refused:
        enforce_inbound_auth(kids, ANONYMOUS)
    assert refused.value.status_code == 401
