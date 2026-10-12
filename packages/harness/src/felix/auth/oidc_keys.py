"""An external issuer's published signing keys, fetched on demand and cached.

Shared by the logins that verify someone else's ID token — GitHub Actions
(`felix.auth.github_actions`) and Google (`felix.auth.google`). The keys are the issuer's, not a
tenant's, so each cache is a module global in the login that owns it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from joserfc import jwk

KEYS_TTL_S = 15 * 60
# Fetches of an issuer's key set are at least this far apart, however they are triggered: anyone
# can present a token naming a key we do not hold, or arrive while the first fetch is failing.
KEYS_REFETCH_MIN_S = 60


@dataclass
class JwksCache:
    """One issuer's key set. `unavailable` builds the login's own error from a log detail."""

    url: str
    label: str
    unavailable: Callable[[str], Exception]
    keys: Any = None
    fetched_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def age(self) -> float:
        return float("inf") if self.fetched_at is None else time.monotonic() - self.fetched_at

    async def get(self, http: httpx.AsyncClient, *, refresh: bool = False) -> Any:
        """The key set, fetched when missing, stale or `refresh`ed — and never twice a minute.

        Single-flight: requests arriving while a fetch is out wait for it rather than each
        starting their own.
        """
        async with self.lock:
            wanted = refresh or self.keys is None or self.age() > KEYS_TTL_S
            if wanted and self.age() >= KEYS_REFETCH_MIN_S:
                self.fetched_at = time.monotonic()
                self.keys = await self._fetch(http)
            if self.keys is None:
                raise self.unavailable(f"{self.label} JWKS not fetched yet; the last attempt failed")
            return self.keys

    async def _fetch(self, http: httpx.AsyncClient) -> Any:
        try:
            resp = await http.get(self.url, headers={"accept": "application/json"})
        except httpx.HTTPError as exc:
            raise self.unavailable(f"{self.label} JWKS unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise self.unavailable(f"{self.label} JWKS answered {resp.status_code}")
        try:
            return jwk.KeySet.import_key_set(resp.json())
        except Exception as exc:
            raise self.unavailable(f"{self.label} JWKS did not parse") from exc
