"""`POST /connections/icloud`: a person connects their iCloud with an app-specific password.

Called as the person, with their Apple ID and an app-specific password they made at
appleid.apple.com. The pair is checked with Apple first -- the CalDAV principal lookup the tools
start from -- and kept only if Apple accepts it, sealed in the person's vault (provider `icloud`,
kind `secret`, account the Apple ID). Listing and disconnecting are the vault's own routes
(`GET /connections`, `DELETE /connections/icloud/{apple_id}`); disconnecting forgets the password
here, and the person revokes it at Apple.

Mounted by the plugin only with the `icloud` extra installed, and answering 404 where the vault
has no sealing key. Each person may try `ATTEMPTS` times an hour, so the route is no way to test
Apple credentials in bulk from this deployment's address. The password is never logged, returned
or put in an error: the body is read here rather than by FastAPI, whose 422 echoes what it was
sent, and a refusal is one of a few written codes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from felix_connectors.connect import ConnectionView, refused
from felix_connectors.icloud_dav import PROVIDER, Dav, Refused, basic, check_app_password, check_apple_id

logger = logging.getLogger("felix_connectors.icloud_connect")

MOUNT = "/connections/icloud"
ATTEMPTS = 5
WINDOW_S = 3600


class ICloudConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apple_id: str = Field(
        min_length=1, max_length=320, description="The email the person signs in to Apple with."
    )
    app_password: SecretStr = Field(
        min_length=1, max_length=64, description="An app-specific password from appleid.apple.com."
    )


async def _credential(request: Request) -> tuple[str, str] | JSONResponse:
    """(Apple ID, app-specific password) from the body, checked, or the written refusal."""
    try:
        body = ICloudConnect.model_validate(json.loads(await request.body()))
    except ValueError, ValidationError:
        return refused("invalid_body", "send {apple_id, app_password} as JSON")
    try:
        apple_id = check_apple_id(body.apple_id)
    except ValueError:
        return refused("apple_id_invalid", "an Apple ID is the email you sign in to Apple with")
    try:
        return apple_id, check_app_password(body.app_password.get_secret_value())
    except ValueError:
        return refused(
            "app_password_format", "use an app-specific password from appleid.apple.com (xxxx-xxxx-xxxx-xxxx)"
        )


def build_router() -> APIRouter:
    router = APIRouter(tags=["Connections"])

    @router.post(
        "",
        response_model=ConnectionView,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ICloudConnect.model_json_schema()}},
            }
        },
    )
    async def connect_icloud(request: Request) -> Any:
        """Connect your iCloud (Calendar, Contacts) with an app-specific password."""
        from felix.auth import connections
        from felix.auth.mgmt import request_person

        settings = request.app.state.settings
        if not connections.store_enabled(settings):
            raise HTTPException(status_code=404, detail="not_found")
        tenant, person = request_person(request)
        if not await request.app.state.icloud_limiter.hit(
            f"icloud-connect:{tenant}:{person}", limit=ATTEMPTS, window_seconds=WINDOW_S
        ):
            return refused("rate_limited", "too many tries; wait an hour and try again", 429)
        checked = await _credential(request)
        if isinstance(checked, JSONResponse):
            return checked
        apple_id, password = checked
        dav: Dav = request.app.state.icloud_dav
        try:
            await dav.principal(dav.caldav, basic(apple_id, password))
        except Refused:
            return refused(
                "app_password_refused", "Apple did not accept this Apple ID and app-specific password"
            )
        except Exception as exc:
            logger.warning("icloud connect check failed error=%s", type(exc).__name__)
            return refused("icloud_unavailable", "iCloud could not be reached just now; try again", 502)
        try:
            stored = await connections.save_secret(
                settings,
                tenant,
                person=person,
                provider=PROVIDER,
                account=apple_id,
                secret=password,
                label=apple_id,
            )
        except connections.StoreUnavailable:
            return refused("connect_failed", "connecting did not finish; try again", 503)
        return ConnectionView.model_validate(connections.public_view(stored))

    return router


def mount(app: Any) -> None:
    """The route, when the `icloud` extra is installed: without it there are no tools to use it."""
    from felix.security.rate_limit import build_rate_limiter_backend

    from felix_connectors.icloud_dav import require_parsers

    try:
        require_parsers()
    except ValueError:
        logger.info("icloud connect not mounted: the felix-connectors[icloud] extra is not installed")
        return
    settings = app.state.settings
    app.state.icloud_dav = Dav.from_settings(settings)
    # Its own store, as the channels plugin's: one window length per store (`rate_limit`).
    app.state.icloud_limiter = build_rate_limiter_backend(settings)
    app.include_router(build_router(), prefix=MOUNT)


__all__ = ["ATTEMPTS", "MOUNT", "ICloudConnect", "build_router", "mount"]
