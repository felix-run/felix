"""What every inbound screener shares: the verdict type, its limits, and how a verdict lands.

Below `inbound` and `image_screening` both, so neither reaches into the other. The scorers
themselves stay in `inbound`, which is where tests replace them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from felix.manifests.schema import Manifest


class InboundScreeningError(PermissionError):
    """User/input content failed screening."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ScreenResult:
    """Tri-state screening outcome.

    ``None`` used to mean both "clean" and "the screener could not run", and every call
    site read it as clean — so a missing API key, an expired credential, a 429, or a
    provider outage silently turned ``content_screening.on_flag: block`` into a no-op,
    with ``logger.debug`` as the only signal.
    """

    score: float | None = None
    available: bool = True
    reason: str = ""

    @property
    def flagged(self) -> bool:
        return self.available and self.score is not None and self.score >= INJECTION_THRESHOLD

    @property
    def unavailable(self) -> bool:
        return not self.available


# Text beyond this is not seen by the model screener; a payload placed past it would
# otherwise be silently unscreened.
SCREEN_CHARS = 4000
INJECTION_THRESHOLD = 0.8


def note_screening(manifest: Manifest, surface: str, action: str) -> None:
    """A screening decision is a governance event: a counter and an audit row, no content."""
    from felix.audit.emit import emit_agent_audit
    from felix.observability.metrics import record_counter

    name = manifest.metadata.name
    record_counter("felix_inbound_screening", {"manifest_id": name, "surface": surface, "action": action})
    emit_agent_audit("inbound_screening", status=action, payload={"surface": surface}, manifest_id=name)


def settle_screening(
    manifest: Manifest,
    surface: str,
    outcome: Literal["flagged", "unavailable", "oversize"],
    *,
    error: str,
    status_code: int,
    refuse: bool | None = None,
) -> None:
    """Record a screening decision, and refuse the request when `on_flag` is `block`.

    Returns under `quarantine`, and the caller substitutes its own `[quarantined]` note. One
    place for the rule, because the copies of it had begun to disagree about the status an
    oversize input earns and which audit action it records.

    `refuse=False` quarantines whatever `on_flag` says: for content replayed from a session,
    where refusing would refuse every later turn of an append-only thread.
    """
    block = manifest.spec.content_screening.on_flag == "block" if refuse is None else refuse
    action = ("denied" if block else "quarantined") if outcome == "flagged" else outcome
    note_screening(manifest, surface, action)
    if block:
        raise InboundScreeningError(error, status_code=status_code)


# The model screener reads SCREEN_CHARS at a time; a turn or argument set longer than
# this many chunks is refused rather than screened, because each chunk is a model call
# and rate limiting counts requests, not calls.
MAX_SCREEN_CHUNKS = 8


# Windows overlap by this much so a payload straddling a boundary is inside one of them.
SCREEN_OVERLAP = 200


__all__ = [
    "INJECTION_THRESHOLD",
    "MAX_SCREEN_CHUNKS",
    "SCREEN_CHARS",
    "SCREEN_OVERLAP",
    "InboundScreeningError",
    "ScreenResult",
    "note_screening",
    "settle_screening",
]
