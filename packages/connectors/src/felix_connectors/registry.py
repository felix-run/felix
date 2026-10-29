"""The open registry each connector family selects its backend from.

One shape for every family, so maps and market data do not each copy the weather one and pick
their own answer to an unknown name. That answer is: refused, by name, with the registered ones
listed -- when a tool of the family is first built, which is the first compile that names one.
Not degraded to a tool that answers every call with an error, which reads to a model as an
outage to retry; and not at boot, because a plugin that raises while loading is logged and
skipped, which would drop every connector tool instead of the misconfigured family.
"""

from __future__ import annotations

from collections.abc import Callable

from felix.config import Settings


class BackendRegistry[T]:
    def __init__(self, setting: str) -> None:
        #: The `FELIX_*` variable that names the backend, for the refusal.
        self.setting = setting
        self._factories: dict[str, Callable[[Settings], T]] = {}

    def register(self, name: str, factory: Callable[[Settings], T]) -> None:
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._factories)

    def build(self, settings: Settings, name: str) -> T:
        factory = self._factories.get(name)
        if factory is None:
            raise ValueError(f"unknown {self.setting} {name!r} (registered: {', '.join(self.names())})")
        return factory(settings)


__all__ = ["BackendRegistry"]
