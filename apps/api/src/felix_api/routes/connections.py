"""A person's stored connections to other services, and an operator's view of everyone's.

`GET /connections` and `DELETE /connections/{provider}/{account}` are the caller's own: who they
have connected, and a way to disconnect, which withdraws the grant at the provider too. The
tenant's listing and revocation (`/connections/tenant`) need `connections:admin`. No route here
returns a credential; storing one is the business of each provider's consent flow
(`felix.auth.connections`).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["Connections"])


class ConnectionOut(BaseModel):
    """Who, what and whether it still works. Never a credential."""

    provider: str
    account: str
    label: str
    kind: Literal["oauth", "secret"]
    status: Literal["active", "revoked"]
    scopes: list[str]
    created_at: int
    updated_at: int
    # When the stored grant itself expires (epoch ms); 0 when the provider did not say.
    expires_at: int


class ConnectionList(BaseModel):
    items: list[ConnectionOut]


class TenantConnectionOut(ConnectionOut):
    # `issuer|subject`.
    person: str


class TenantConnectionList(BaseModel):
    items: list[TenantConnectionOut]


class RevokeConnection(BaseModel):
    person: str = Field(min_length=1, description="`issuer|subject`, as the tenant listing shows it.")
    provider: str = Field(min_length=1)
    account: str = Field(min_length=1)


def _person(request: Request) -> tuple[str, str]:
    """The caller's tenant and person key (`issuer|subject`); 403 for a caller that names no one
    person -- anonymous, a sub-less API key, a workflow, a machine actor."""
    from felix.auth.mgmt import auth_from_request
    from felix.auth.persons import caller_person

    auth = auth_from_request(request)
    principal = auth.principal
    person = caller_person(issuer=principal.issuer, subject=principal.subject or "", anonymous=auth.anonymous)
    if not person:
        raise HTTPException(status_code=403, detail="not_a_person")
    return principal.tenant_id, person


@router.get("", response_model=ConnectionList)
async def my_connections(request: Request) -> ConnectionList:
    """The services you have connected in this tenant."""
    from felix.auth import connections

    tenant, person = _person(request)
    rows = await connections.list_for_person(request.app.state.settings, tenant, person)
    return ConnectionList(items=[ConnectionOut.model_validate(r) for r in rows])


@router.get("/tenant", response_model=TenantConnectionList)
async def tenant_connections(request: Request) -> TenantConnectionList:
    """Every stored connection in your tenant, newest first (`connections:admin`)."""
    from felix.auth import connections
    from felix.auth.mgmt import auth_from_request, require_mgmt_scopes

    require_mgmt_scopes(request, "connections:admin")
    tenant = auth_from_request(request).principal.tenant_id
    rows = await connections.list_connections(request.app.state.settings, tenant)
    return TenantConnectionList(items=[TenantConnectionOut.model_validate(r) for r in rows])


@router.post("/tenant/revoke", status_code=204)
async def revoke_connection(body: RevokeConnection, request: Request) -> None:
    """Remove anyone's connection in your tenant and withdraw its grant (`connections:admin`).
    A body rather than a path: a subject or an account may hold any character."""
    from felix.auth import connections
    from felix.auth.mgmt import auth_from_request, require_mgmt_scopes

    require_mgmt_scopes(request, "connections:admin")
    principal = auth_from_request(request).principal
    found = await connections.remove_connection(
        request.app.state.settings,
        principal.tenant_id,
        body.person,
        body.provider,
        body.account,
        actor=principal.subject or "",
    )
    if not found:
        raise HTTPException(status_code=404, detail="not_found")


@router.delete("/{provider}/{account}", status_code=204)
async def remove_my_connection(provider: str, account: str, request: Request) -> None:
    """Disconnect one of your accounts here, and withdraw Felix's grant at the provider."""
    from felix.auth import connections

    tenant, person = _person(request)
    found = await connections.remove_connection(
        request.app.state.settings, tenant, person, provider, account, actor=person
    )
    if not found:
        raise HTTPException(status_code=404, detail="not_found")
