"""Tool sources a run was given and could not use: what the model is told, and what the run records.

A source that cannot be reached when a run compiles -- an MCP server that is down, or a stdio one that
cannot start -- binds no tools. Unsaid, nothing else changes: an agent asked "what is new in the
tracker" finds no tracker tools and answers "nothing new", an outage reported as a quiet day, and the
run records `ok`. So the binder that lost a source says so twice: a note in the agent's system prompt
(`unavailable_sources_note`), and a record on the run (`record_unavailable_sources`), which a job's
run result carries.

A source the manifest marks `required: true` is not noted but refused: the compile raises
`RequiredSourceUnavailable`, so the run stops before any model call -- a job's run is an error,
a request answers 503 -- instead of running without what it cannot do without.
"""

from __future__ import annotations

import re

#: `RequestContext.extras` key: `{manifest: [source, ...]}`, the sources each agent compiled for the
#: run could not reach.
UNAVAILABLE_SOURCES_EXTRA = "unavailable_sources"
#: The tag the prompt note is wrapped in.
UNAVAILABLE_SOURCES_TAG = "unavailable_sources"
_UNSAFE = re.compile(r"[<>`\r\n]")
#: How `RequiredSourceUnavailable`'s message begins; the job health watch classes an error by it.
REQUIRED_SOURCES_UNAVAILABLE = "required tool sources unavailable"


def _shown(name: str) -> str:
    return f"`{_UNSAFE.sub('', name)}`"


class RequiredSourceUnavailable(Exception):
    """A source the manifest marks `required: true` could not be used for this run: it could not be
    reached or started, it answered with no tools to bind, or it acts as a person and the run has no
    person or connection for it (or the operator no longer lets it be sent one).

    Raised by the compile, so the run stops before any model call. Its message is written for the
    caller and is relayed (`felix.errors._relayable`): it names the manifest and its servers -- the
    manifest author's own names, cleaned as the prompt note cleans them -- and nothing a server said.
    """

    #: The refusal's code on a surface that types its errors (an SSE error frame, `/v1`'s `code`).
    code = "required_source_unavailable"

    def __init__(self, manifest_id: str, names: list[str]) -> None:
        self.manifest_id = manifest_id
        self.names = list(names)
        listed = ", ".join(_shown(n) for n in self.names)
        # The names last: an SSE error frame keeps 200 characters, and a cut should lose a name, not
        # the sentence that says what happened.
        super().__init__(
            f"{REQUIRED_SOURCES_UNAVAILABLE} for {_shown(manifest_id)} (could not be reached, started "
            f"or connected, or listed no tools), so the run did not start: {listed}"
        )


def unavailable_sources_note(names: list[str]) -> str:
    """What the model is told about the sources in ``names``, which are the manifest's own. The
    wording only: recording them is `record_unavailable_sources`. A name loses the characters that
    could close its backticks or the tag; it is the manifest author's, who writes the prompt anyway,
    but the tag should say only what it is for."""
    listed = ", ".join(_shown(n) for n in names)
    return (
        f"<{UNAVAILABLE_SOURCES_TAG}>These tool sources could not be reached or started when this run "
        f"began, so their tools are missing: {listed}. What you would have read from them is unknown, "
        "not empty: never report it as having nothing new. Work from the sources you do have, and say "
        f"plainly which ones you could not read.</{UNAVAILABLE_SOURCES_TAG}>"
    )


def record_unavailable_sources(manifest_id: str, names: list[str]) -> None:
    """Record ``names`` on the run under ``manifest_id``: a run compiles its children too, and a
    router whose `billing` child lost its server should not read as the router's outage. Nothing
    outside a request."""
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is None:
        return
    seen = ctx.extras.setdefault(UNAVAILABLE_SOURCES_EXTRA, {}).setdefault(manifest_id, [])
    seen.extend(n for n in names if n not in seen)


def unavailable_sources(extras: dict[str, object]) -> dict[str, list[str]]:
    """The run's record, copied out: `{}` when every source answered."""
    recorded = extras.get(UNAVAILABLE_SOURCES_EXTRA)
    if not isinstance(recorded, dict):
        return {}
    return {str(m): list(s) for m, s in recorded.items()}


__all__ = [
    "REQUIRED_SOURCES_UNAVAILABLE",
    "UNAVAILABLE_SOURCES_EXTRA",
    "UNAVAILABLE_SOURCES_TAG",
    "RequiredSourceUnavailable",
    "record_unavailable_sources",
    "unavailable_sources",
    "unavailable_sources_note",
]
