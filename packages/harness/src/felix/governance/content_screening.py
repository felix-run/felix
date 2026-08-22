"""Content screening for inbound queue write-backs and untrusted tool output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from felix.config import Settings

_INJECTION = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+your\s+(system\s+)?prompt", re.I),
    re.compile(r"system\s+prompt\s*:", re.I),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
)


@dataclass(slots=True)
class ScreeningVerdict:
    denied: bool = False
    reason: str = ""
    redacted: str | None = None


async def screen_content(
    text: str,
    *,
    settings: Settings | None = None,
    block_on_injection: bool = True,
    redact_pii: bool = True,
    **kwargs: Any,
) -> ScreeningVerdict:
    _ = (settings, kwargs)
    if not text:
        return ScreeningVerdict(denied=False)

    if block_on_injection:
        for rx in _INJECTION:
            if rx.search(text):
                return ScreeningVerdict(denied=True, reason="prompt_injection_marker")

    if redact_pii:
        from felix.governance.pii import redact_pii as _redact

        result = _redact(text)
        if result.matched and result.text != text:
            return ScreeningVerdict(denied=False, reason="pii_redacted", redacted=result.text)

    return ScreeningVerdict(denied=False)


__all__ = ["ScreeningVerdict", "screen_content"]
