"""The step every text an agent sends out of the harness under the deployment's name goes through.

`notify` (`push/agent_tool.py`) and `post` (`destinations/tool.py`) carry text a model wrote --
possibly under a prompt injection -- to a person's lock screen or a team's channel. Neither can
quarantine what it sends the way the screening wrapper quarantines a tool's output, so both prepare
it the same way, in one order (`prepare_outbound`):

1. known secret values are redacted (the governance stack masks a tool's *output*; this text is its
   *arguments*);
2. links are handled by the caller's policy: `strip_links` removes them -- scheme URLs, `www.`
   hosts, bare `host.tld/path` in any script, a host with a port, IPv4 and IPv6 addresses, and the
   common obfuscations (`hxxp`, `[.]`, `(dot)`, `%2E`, lookalike dots, a host split across lines)
   -- after NFKC normalization and with invisible format characters deleted, and can keep the runs
   whose host a predicate accepts (`link_host`);
3. the result is screened (`screen_outbound`): always the free marker scan
   (`content_screening.py`), then, when the manifest's content screening is on and covers the tool,
   its model and decider as the screening wrapper runs them. A flag refuses the call, and so does a
   screener that cannot run: unscreened is not clean.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from felix.tools.refusal import ToolRefused

if TYPE_CHECKING:
    from felix.manifests.schema import Manifest

logger = logging.getLogger("felix.governance.outbound_text")

LINK_REMOVED = "[link removed]"

# --- links ---------------------------------------------------------------------------------------
# A dot, or one of the ways people write one to get past a filter: the ideographic, fullwidth,
# halfwidth, one-dot-leader and small full stops, percent-encoding, and the bracketed and spelled-out
# forms. NFKC folds most of the Unicode ones to `.` first; they are listed so the class does not
# depend on it.
_DOT = r"(?:\.|。|．|｡|․|﹒|%2e|\[\.\]|\(\.\)|\{\.\}|\s*\[dot\]\s*|\s*\(dot\)\s*|\s+dot\s+)"
# A label in any script: a letter or digit, then letters, digits or hyphens.
_LABEL = r"[^\W_](?:[^\W_]|-){0,62}"
# A top-level domain: letters only, in any script, so versions (`1.4`, `v2.0.1`) never end in one.
_TLD = r"[^\W\d_]{2,24}"
_TAIL = r"(?:[:/?#][^\s]*)?"
_HEX = r"[0-9a-f]{1,4}"
_LINKS = (
    # Anything with a scheme: https://, hxxps://, ftp://, an app's own scheme; `http://3232235777`.
    re.compile(r"\b[a-z][a-z0-9+.-]{0,30}://[^\s]*", re.I),
    re.compile(r"\b(?:javascript|data|mailto|tel|sms|intent):[^\s]+", re.I),
    re.compile(rf"\bwww{_DOT}[^\s]+", re.I),
    # IPv4, dotted with decimal or hex parts, and a whole address written as one hex number.
    re.compile(
        rf"\b(?:0x[0-9a-f]{{1,8}}|\d{{1,3}})(?:{_DOT}(?:0x[0-9a-f]{{1,8}}|\d{{1,3}})){{3}}\b{_TAIL}", re.I
    ),
    re.compile(r"\b0x[0-9a-f]{8}\b", re.I),
    # IPv6: bracketed, compressed (it has a `::`), or all eight groups.
    re.compile(rf"\[[0-9a-f:.]*:[0-9a-f:.]*\]{_TAIL}", re.I),
    re.compile(
        rf"(?<![\w:])(?:{_HEX}(?::{_HEX}){{0,6}})?::(?:{_HEX}(?::{_HEX}){{0,6}})?(?![\w:]){_TAIL}", re.I
    ),
    re.compile(rf"\b{_HEX}(?::{_HEX}){{7}}\b{_TAIL}", re.I),
    # A host split across lines (`evil.` / `com`). The TLD is lowercase only here, so a sentence
    # ending a line and the next beginning with a capital is not taken for one.
    re.compile(rf"\b(?:{_LABEL}{_DOT})*{_LABEL}\.[\r\n]+[a-z]{{2,24}}\b{_TAIL}"),
    # A bare host: two or more labels ending in an alphabetic TLD, and any path after it. This
    # takes some filenames (`notes.md`) with it; a notice that loses one is cheaper than one that
    # carries a link.
    re.compile(rf"\b(?:{_LABEL}{_DOT})+{_TLD}\b{_TAIL}", re.I),
    # A single-label host with a port (`localhost:8080/x`). The label must start with a letter and
    # the port have two digits or more, so a time (`10:30`) is not one.
    re.compile(r"\b[^\W\d_](?:[^\W_]|-){0,62}:\d{2,5}\b(?:/[^\s]*)?", re.I),
    re.compile(r"\blocalhost(?:/[^\s]*)?", re.I),
)


def _normalize(text: str) -> str:
    """NFKC (fullwidth letters and dots become plain ones), with every invisible format character
    -- zero-width joiners, direction marks, the soft hyphen -- deleted, so none can hide a host."""
    import unicodedata

    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if c != "\u00ad" and unicodedata.category(c) != "Cf")


def strip_links(text: str, keep: Callable[[str], bool] | None = None) -> str:
    """`text`, normalized, with every link-shaped run replaced by `[link removed]` -- except, with
    ``keep``, a run whose host ``keep`` accepts (`link_host`), which stays as written. Each pattern
    asks again of what it matches, so a link kept by one is judged afresh by the next; an obfuscated
    run (`evil[.]com`) has no host to accept and is removed."""
    text = _normalize(text)
    for rx in _LINKS:
        if keep is None:
            text = rx.sub(LINK_REMOVED, text)
        else:
            text = rx.sub(lambda m: m.group(0) if keep(link_host(m.group(0))) else LINK_REMOVED, text)
    return text


def link_host(run: str) -> str:
    """The host a link-shaped run names, as a request would carry it (`url_host`), or `""` for one
    with no host (`mailto:`, `javascript:`, an obfuscated name). A run with no scheme is read as
    `https://` plus the run, so `docs.example.com/x` and `www.example.com` have their hosts too."""
    from felix.security.host_allowlist import url_host

    candidate = run if "://" in run else f"https://{run}"
    return url_host(candidate)


async def screen_outbound(
    settings: Any, manifest: Manifest, tool_name: str, text: str, *, flagged: str, unavailable: str
) -> None:
    """Raise `ToolRefused("screened", ...)` -- `flagged` when a screener flags `text`, `unavailable`
    when one that should run cannot -- or return when it is clean. The messages are the tool's own."""
    from felix.governance.content_screening import screen_content

    verdict = await screen_content(text, settings=settings, block_on_injection=True, redact_pii=False)
    if verdict.denied:
        raise ToolRefused("screened", flagged)
    screening = manifest.spec.content_screening
    if not screening.enabled:
        return
    from felix.manifests.tool_match import matches_any

    if screening.model_tools and not matches_any(screening.model_tools, tool_name):
        return
    from felix.governance.screening import screening_decider
    from felix.governance.tool_screening import screen_tool_output

    try:
        decider = screening_decider(manifest, settings)
    except Exception:
        logger.warning(
            "%s screening decider unavailable manifest=%s", tool_name, manifest.metadata.name, exc_info=True
        )
        raise ToolRefused("screened", unavailable) from None
    model_id = screening.model.strip()
    if not (model_id or decider is not None):
        return
    result = await screen_tool_output(settings, text, model_id, decider)
    if result.unavailable:
        raise ToolRefused("screened", unavailable)
    if result.flagged:
        raise ToolRefused("screened", flagged)


async def prepare_outbound(
    settings: Any,
    manifest: Manifest,
    tool_name: str,
    parts: Sequence[str],
    *,
    links: Callable[[str], str],
    flagged: str,
    unavailable: str,
) -> list[str]:
    """``parts`` (a title, a body) as they would leave: each with known secrets redacted, then put
    through ``links`` (`strip_links`, or a destination's own policy), then screened together, the
    non-empty ones joined by newlines -- or `ToolRefused("screened", ...)` with the tool's own
    ``flagged`` / ``unavailable`` message."""
    from felix.secrets import collected_secret_values, redact_text

    secrets = collected_secret_values(settings)
    cleaned = [links(redact_text(part, secrets)) for part in parts]
    await screen_outbound(
        settings,
        manifest,
        tool_name,
        "\n".join(p for p in cleaned if p),
        flagged=flagged,
        unavailable=unavailable,
    )
    return cleaned


__all__ = ["LINK_REMOVED", "link_host", "prepare_outbound", "screen_outbound", "strip_links"]
