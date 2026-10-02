"""Log in to a Felix server with GitHub, and keep the token between runs.

`github_device_login` drives `POST /auth/github/device` and `POST /auth/github/token`: it
shows the person a code (through `on_code`), polls at the interval the server asks for, and
returns a bearer token once they approve on github.com. Nothing here talks to GitHub directly;
the server holds the OAuth app and checks org membership.

A device code is single-use. If the server answers `tenant_ambiguous` (the person's orgs map to
more than one tenant), that flow is spent: start a new one passing `tenant`.

Saved tokens live in one file, one per server, and a token is only ever handed back for the
server that minted it (`bearer_for`, which `FelixClient.from_login` and `clients/cli.py` use).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# RFC 8628 §3.5: on `slow_down` the client adds five seconds to its polling interval.
SLOW_DOWN_STEP_SECONDS = 5
# The server chooses the cadence and the deadline; a client still bounds them. An interval of 0
# is a busy loop and one of 10^9 a hung terminal. GitHub's own codes last 900 s.
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 60
MAX_EXPIRES_IN_SECONDS = 1800

TOKEN_FILE_VERSION = 1
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """What to show the person: enter `user_code` at `verification_uri`."""

    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class LoginToken:
    # Out of the repr: a token in a log line or a traceback's locals is a token handed out.
    access_token: str = field(repr=False)
    tenant: str
    scopes: tuple[str, ...]
    expires_at: float
    base_url: str

    def expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def to_json(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "scopes": list(self.scopes)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LoginToken:
        """Raises KeyError / TypeError / ValueError on a malformed entry."""
        return cls(
            access_token=str(data["access_token"]),
            tenant=str(data["tenant"]),
            scopes=tuple(str(s) for s in data.get("scopes") or ()),
            expires_at=float(data["expires_at"]),
            base_url=str(data["base_url"]),
        )


class LoginError(Exception):
    """The login did not produce a token. `code` is the server's `error` field, or one of this
    client's own (`insecure_url`, `server_unreachable`, `bad_response`, `expired_token`);
    `tenants` accompanies `tenant_ambiguous`, `interval` a 428 or 429."""

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


class TokenFileError(Exception):
    """The token file or its directory is not private to this user, so it is not used."""


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


def _ok_body(resp: httpx.Response) -> dict[str, Any]:
    """A 200's JSON object, or `bad_response`: a captive portal or proxy answers 200 with HTML."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise LoginError(
            "bad_response", f"{resp.url} answered 200 with something other than JSON", status=200
        )
    return body


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _printable(text: str) -> str:
    """Server-supplied text shown in a terminal: no escape sequences to disguise what it says."""
    return "".join(ch for ch in text if ch.isprintable())


def check_url(base_url: str, *, allow_insecure: bool = False) -> str:
    """`base_url` without its trailing slash, or `insecure_url`: a bearer token comes back over
    this connection, so plain HTTP is for loopback unless the caller says otherwise."""
    url = httpx.URL(base_url)
    if url.scheme == "https" or allow_insecure:
        return base_url.rstrip("/")
    if url.scheme == "http" and url.host in _LOOPBACK_HOSTS:
        return base_url.rstrip("/")
    raise LoginError(
        "insecure_url",
        f"{base_url} is not https; the token would cross the network in cleartext",
        status=0,
    )


async def github_device_login(
    base_url: str,
    *,
    tenant: str | None = None,
    on_code: Callable[[DeviceCode], Any] | None = None,
    timeout: float = 30.0,
    allow_insecure: bool = False,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> LoginToken:
    """Run one GitHub device-flow login against a Felix server and return its token.

    `on_code` receives the code to show (a coroutine function is awaited); without one it is
    printed. Raises `LoginError` on every way this can fail. `sleep` and `clock` are the waiting
    and the deadline; a test replaces them together.
    """
    base = check_url(base_url, allow_insecure=allow_insecure)
    async with contextlib.AsyncExitStack() as stack:
        http = client or await stack.enter_async_context(httpx.AsyncClient(timeout=timeout))
        started = await _post(http, f"{base}/auth/github/device")
        if started.status_code != 200:
            raise _refusal(started)
        data = _ok_body(started)
        try:
            device_code = str(data["device_code"])
            code = DeviceCode(
                user_code=_printable(str(data["user_code"])),
                verification_uri=_printable(str(data["verification_uri"])),
                expires_in=_clamp(int(data["expires_in"]), 1, MAX_EXPIRES_IN_SECONDS),
                interval=_clamp(int(data["interval"]), MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LoginError("bad_response", "the device code answer was incomplete", status=200) from exc
        shown = (on_code or _print_code)(code)
        if inspect.isawaitable(shown):
            await shown

        body: dict[str, Any] = {"device_code": device_code}
        if tenant:
            body["tenant"] = tenant
        interval = code.interval
        deadline = clock() + code.expires_in
        while True:
            if clock() + interval > deadline:
                raise LoginError("expired_token", "the code expired before it was approved", status=400)
            await sleep(interval)
            resp = await _post(http, f"{base}/auth/github/token", json=body)
            if resp.status_code == 200:
                return _token_from(_ok_body(resp), base)
            refused = _refusal(resp)
            if refused.code == "slow_down":
                interval = _clamp(
                    refused.interval or interval + SLOW_DOWN_STEP_SECONDS,
                    MIN_INTERVAL_SECONDS,
                    MAX_INTERVAL_SECONDS,
                )
            elif refused.code != "authorization_pending":
                raise refused


def _token_from(out: dict[str, Any], base: str) -> LoginToken:
    try:
        return LoginToken.from_json(
            {**out, "expires_at": time.time() + int(out["expires_in"]), "base_url": base}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LoginError("bad_response", "the token answer was incomplete", status=200) from exc


async def _post(http: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """One exception type for every way a login can fail, network included."""
    try:
        return await http.post(url, **kwargs)
    except httpx.TransportError as exc:
        raise LoginError("server_unreachable", f"could not reach {url}: {exc}", status=0) from exc


def _print_code(code: DeviceCode) -> None:
    print(f"Open {code.verification_uri} and enter {code.user_code}")


# --- the saved tokens ---------------------------------------------------------------------
#
# {"version": 1, "tokens": {"<base_url>": {access_token, tenant, scopes, expires_at, base_url}}}
#
# The file is a bearer credential, so it is only trusted when it is plainly this user's: a
# regular file (not a symlink) owned by them with no group or other bits, in a directory that
# is the same. A shared `XDG_CONFIG_HOME` is where someone else could plant a token for their
# own tenant, or a symlink to catch ours.


def token_path() -> Path:
    """`$XDG_CONFIG_HOME/felix/token`, else `~/.config/felix/token`."""
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "felix" / "token"


def _private(st: os.stat_result) -> bool:
    owned = not hasattr(os, "getuid") or st.st_uid == os.getuid()
    return owned and stat.S_IMODE(st.st_mode) & 0o077 == 0


def _private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode) or not _private(st):
        raise TokenFileError(
            f"{directory} must be a directory owned by you with mode 0700 (chmod 700 {directory})"
        )


def _read_tokens(target: Path) -> dict[str, dict[str, Any]]:
    """The saved entries, or nothing when the file is missing, malformed or not private."""
    try:
        st = target.lstat()
    except OSError:
        return {}
    if not stat.S_ISREG(st.st_mode) or not _private(st):
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict) or data.get("version") != TOKEN_FILE_VERSION:
        return {}
    tokens = data.get("tokens")
    return {k: v for k, v in tokens.items() if isinstance(v, dict)} if isinstance(tokens, dict) else {}


def save_token(token: LoginToken, path: Path | None = None) -> Path:
    """Add (or replace) this server's token, keeping other servers' unexpired ones.

    Written to a fresh 0600 file beside the target and renamed over it, so there is no moment
    at which the token sits in a wider file, a symlink at the target is replaced rather than
    followed, and a crash leaves the old file whole. Raises `TokenFileError` for a directory
    that is not private to this user.
    """
    target = path or token_path()
    _private_dir(target.parent)
    now = time.time()
    tokens = {
        base: entry
        for base, entry in _read_tokens(target).items()
        if isinstance(entry.get("expires_at"), (int, float)) and entry["expires_at"] > now
    }
    tokens[token.base_url] = token.to_json()
    payload = json.dumps({"version": TOKEN_FILE_VERSION, "tokens": tokens})
    tmp = target.parent / f".{target.name}.{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def load_token(base_url: str, path: Path | None = None) -> LoginToken | None:
    """The saved token for `base_url`, or None when there is none for that server, it has
    expired, or the file is not private to this user. A token is never handed back for a server
    other than the one that minted it."""
    base = base_url.rstrip("/")
    entry = _read_tokens(path or token_path()).get(base)
    if entry is None:
        return None
    try:
        token = LoginToken.from_json(entry)
    except KeyError, TypeError, ValueError:
        return None
    if token.base_url != base or token.expired():
        return None
    return token


def bearer_for(base_url: str, explicit: str | None = None, path: Path | None = None) -> str | None:
    """The bearer to send to `base_url`: an explicit one wins, else that server's saved login."""
    if explicit:
        return explicit
    saved = load_token(base_url, path)
    return saved.access_token if saved is not None else None
