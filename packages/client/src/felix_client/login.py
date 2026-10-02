"""Log in to a Felix server with GitHub, and keep the token between runs.

`github_device_login` drives `POST /auth/github/device` and `POST /auth/github/token`: it
shows the person a code (through `on_code`), polls at the interval the server asks for, and
returns a bearer token once they approve on github.com. Nothing here talks to GitHub directly;
the server holds the OAuth app and checks org membership.

A device code is single-use. If the server answers `tenant_ambiguous` (the person's orgs map to
more than one tenant), that flow is spent: start a new one passing `tenant`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# RFC 8628 §3.5: on `slow_down` the client adds five seconds to its polling interval.
SLOW_DOWN_STEP_SECONDS = 5


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """What to show the person: enter `user_code` at `verification_uri`."""

    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class LoginToken:
    access_token: str
    tenant: str
    scopes: tuple[str, ...]
    expires_at: float
    base_url: str

    def expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


class LoginError(Exception):
    """The server refused. `code` is its `error` field; `tenants` accompanies `tenant_ambiguous`,
    `interval` a 428 or 429."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        tenants: tuple[str, ...] = (),
        interval: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.tenants = tenants
        self.interval = interval


def _refusal(resp: httpx.Response) -> LoginError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("error") or f"http_{resp.status_code}")
    tenants = tuple(str(t) for t in body.get("tenants") or ())
    interval = body.get("interval")
    usable = isinstance(interval, int) and not isinstance(interval, bool) and interval > 0
    return LoginError(
        code,
        str(body.get("message") or code),
        status=resp.status_code,
        tenants=tenants,
        interval=interval if usable else None,
    )


async def github_device_login(
    base_url: str,
    *,
    tenant: str | None = None,
    on_code: Callable[[DeviceCode], Any] | None = None,
    timeout: float = 30.0,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> LoginToken:
    """Run one GitHub device-flow login against a Felix server and return its token.

    `on_code` receives the code to show; without one it is printed. Raises `LoginError` on any
    refusal, including the flow expiring before the person approved it. `sleep` and `clock`
    are the waiting and the deadline; a test replaces them together.
    """
    base = base_url.rstrip("/")
    async with contextlib.AsyncExitStack() as stack:
        http = client or await stack.enter_async_context(httpx.AsyncClient(timeout=timeout))
        started = await _post(http, f"{base}/auth/github/device")
        if started.status_code != 200:
            raise _refusal(started)
        data = started.json()
        code = DeviceCode(
            user_code=str(data["user_code"]),
            verification_uri=str(data["verification_uri"]),
            expires_in=int(data["expires_in"]),
            interval=int(data["interval"]),
        )
        (on_code or _print_code)(code)

        body: dict[str, Any] = {"device_code": str(data["device_code"])}
        if tenant:
            body["tenant"] = tenant
        interval = code.interval
        deadline = clock() + code.expires_in
        while True:
            await sleep(interval)
            resp = await _post(http, f"{base}/auth/github/token", json=body)
            if resp.status_code == 200:
                out = resp.json()
                return LoginToken(
                    access_token=str(out["access_token"]),
                    tenant=str(out["tenant"]),
                    scopes=tuple(out.get("scopes") or ()),
                    expires_at=time.time() + int(out["expires_in"]),
                    base_url=base,
                )
            refused = _refusal(resp)
            if refused.code == "slow_down":
                interval = refused.interval or interval + SLOW_DOWN_STEP_SECONDS
            elif refused.code != "authorization_pending":
                raise refused
            if clock() + interval > deadline:
                raise LoginError("expired_token", "the code expired before it was approved", status=400)


async def _post(http: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """One exception type for every way a login can fail, network included."""
    try:
        return await http.post(url, **kwargs)
    except httpx.TransportError as exc:
        raise LoginError("server_unreachable", f"could not reach {url}: {exc}", status=0) from exc


def _print_code(code: DeviceCode) -> None:
    print(f"Open {code.verification_uri} and enter {code.user_code}")


# --- the saved token ----------------------------------------------------------------------


def token_path() -> Path:
    """`$XDG_CONFIG_HOME/felix/token`, else `~/.config/felix/token`."""
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "felix" / "token"


def save_token(token: LoginToken, path: Path | None = None) -> Path:
    """Write the token readable by this user only. Created 0600, never widened after the fact."""
    target = path or token_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(
        {
            "access_token": token.access_token,
            "tenant": token.tenant,
            "scopes": list(token.scopes),
            "expires_at": token.expires_at,
            "base_url": token.base_url,
        }
    )
    # Opened 0600 from the start: writing first and chmod-ing after leaves a window in which a
    # bearer token sits world-readable under the default umask.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.chmod(target, 0o600)  # an existing file keeps its old mode through O_CREAT
    return target


def load_token(base_url: str, path: Path | None = None) -> LoginToken | None:
    """The saved token for `base_url`, or None when there is none, it is for another server,
    or it has expired. A token is never sent to a server other than the one that minted it."""
    target = path or token_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        token = LoginToken(
            access_token=str(data["access_token"]),
            tenant=str(data["tenant"]),
            scopes=tuple(data.get("scopes") or ()),
            expires_at=float(data["expires_at"]),
            base_url=str(data["base_url"]),
        )
    except OSError, ValueError, KeyError, TypeError:
        return None
    if token.base_url != base_url.rstrip("/") or token.expired():
        return None
    return token
