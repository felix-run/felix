"""`notify`: an agent telling the person it acts for something, on their own devices.

Bound by `manifests/builder.py` for a manifest with `spec.notify`, before the governance stack, so
every wrapper applies -- and with `approval: true` an approval rule (`permission_mode.NOTIFY_APPROVAL`) holds
each call until a person has read it, which no permission mode waives
(`permission_mode.UNWAIVABLE_TOOLS`). Off by default: the runs a notice is for -- a scheduled
task's firing, a heartbeat -- have nobody watching to approve anything.

The recipient is not an argument. A notice goes to `AuthContext.acting_for` and only to the
browsers that subject subscribed itself (`push/store.py:list_for_principal`), so a model cannot be
talked into telling someone else. A caller that names no person -- anonymous, or a credential every
holder shares -- is refused rather than broadcast to (`tools/refusal.py:require_person`).

What it sends is text a model wrote, possibly under a prompt injection, landing on a lock screen
with the deployment's authority behind it. So, in order, before anything leaves:

1. known secret values are redacted (the governance stack masks a tool's *output*; a notice's text
   is its *arguments*);
2. links are removed -- scheme URLs, `www.` hosts, bare `host.tld/path` in any script, a host
   with a port, IPv4 and IPv6 addresses, and the common obfuscations (`hxxp`, `[.]`, `(dot)`,
   `%2E`, lookalike dots, a host split across lines) -- each replaced by `[link removed]`, after
   NFKC normalization and with invisible format characters deleted, so a notice cannot carry a
   phishing target;
3. the result is screened: always by the free marker scan imported skills get, then, when the
   manifest's `content_screening` is enabled, by its model and decider too. A hit refuses the call
   (`screened`); a screener that cannot run refuses it too.

The text is then end-to-end encrypted to the person's browser (RFC 8291); the push service carries
ciphertext.

When the notice links to a conversation other than the run's own -- a scheduled task's firing,
pointing at the chat thread the task was scheduled from -- the same cleaned text is also added to
that conversation, before the push goes, so it is there when the person opens the thread whether or
not any browser took the push. It is an entry for the person and never for the model: a `custom`
entry (`notice_entry`), which `session.types.include_in_llm_context` leaves out of every prompt, so
text a model wrote in one run cannot become context, still less instructions, for another run in a
conversation it was never part of. Only into a thread that exists, and never while a run is going
on in it (`_Notices.append`). The run's own thread gets nothing: the conversation already shows the
call and the answer.

Two limits bound a looping or prompted agent. `max_per_hour` per person, per manifest, per tenant,
across every replica (the shared rate-limit store, Redis when configured) is the real bound.
`max_per_run` is a counter on the request context: one context per run and the agents it
delegates to inline, but a fresh one per *resume* of a durable run and per background child or
workflow step, each of which starts its own count. A call reserves its run slot before anything
awaits, so a turn's parallel calls cannot all pass the cap, and gives it back only if the hourly
limit refuses it; past that it counts against both whether or not any browser accepted it -- an
undelivered notice still spent its turn.

Every refusal is a result the model reads, `{"error": <code>, "detail": <message>}`, written here
and never an exception's text.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from felix.governance.permission_mode import NOTIFY_TOOL_NAME
from felix.jobs import origin_thread
from felix.session import idle_append
from felix.tools.refusal import ToolRefused, require_person
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

if TYPE_CHECKING:
    from felix.context import AuthContext, RequestContext
    from felix.manifests.schema import Manifest, NotifySpec
    from felix.security.rate_limit import RateLimiterBackend
    from felix.session.types import AppendableEvent

logger = logging.getLogger("felix.push.agent_tool")

NOTIFY = NOTIFY_TOOL_NAME
MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 500
# The run's count of notices sent, on `RequestContext.extras` (see the module docstring for what a
# "run" is here).
SENT_THIS_RUN_EXTRA = "notify.sent_this_run"
HOUR_S = 3600
# How long a push service holds a notice for a browser that is offline. A notice is not a wait;
# a day late it is still worth reading.
NOTICE_TTL_SECONDS = 12 * 60 * 60
LINK_REMOVED = "[link removed]"
# The `metadata.type` of the entry a notice leaves in the conversation it links to.
NOTICE_ENTRY_TYPE = "notice"
# What became of the notice in the conversation it links to (the result's `thread`), when it was
# not added there.
_THREAD_NOTES = {
    idle_append.RUN_LIVE: "a run is going on in the conversation it links to, so it was not added there",
    idle_append.MISSING: "the conversation it links to does not exist, so it was not added there",
    idle_append.FAILED: "it could not be added to the conversation it links to",
}

# One limiter per process: an in-process one is the counter, so a fresh one per call counts nothing.
_limiter: RateLimiterBackend | None = None

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


class _NotifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Lengths are refused by the handler, with a message for the model, and advertised here.
    title: str = Field(
        description="A short headline for the notice, as it shows on the person's device.",
        json_schema_extra={"minLength": 1, "maxLength": MAX_TITLE_CHARS},
    )
    message: str = Field(
        description=(
            "What to tell the person, in a sentence or two of plain text. Links are removed. It may "
            "show on their lock screen, so leave out anything they would not want seen there."
        ),
        json_schema_extra={"minLength": 1, "maxLength": MAX_MESSAGE_CHARS},
    )


def _rate_limiter(settings: Any) -> RateLimiterBackend:
    global _limiter
    if _limiter is None:
        from felix.security.rate_limit import build_rate_limiter_backend

        _limiter = build_rate_limiter_backend(settings)
    return _limiter


def hourly_key(tenant_id: str, manifest_id: str, person: str) -> str:
    """The rate-limit key for one person's notices from one manifest. Hashed over an unambiguous
    encoding, so no tenant, manifest or subject containing `:` can land on another's budget."""
    digest = hashlib.sha256(json.dumps([tenant_id, manifest_id, person]).encode()).hexdigest()
    return f"notify:{digest}"


def reset_notify_for_tests() -> None:
    """Forget the per-person hourly counts."""
    global _limiter
    _limiter = None


class _Notices:
    """One manifest's notices, bound to its tenant and `spec.notify`. Every refusal raises
    `ToolRefused`; the tool wrapper in `make_notify_tool` turns it into the result."""

    def __init__(self, settings: Any, *, tenant_id: str, manifest: Manifest, spec: NotifySpec) -> None:
        self.settings, self.tenant_id, self.manifest, self.spec = settings, tenant_id, manifest, spec
        self.manifest_id = manifest.metadata.name

    def admit(self, args: _NotifyArgs, *, reserve: bool) -> tuple[RequestContext, AuthContext]:
        """The checks a call makes before it counts against the hour, run by the call itself and,
        first, by its approval preview.

        With ``reserve`` (the call, not the preview) the run's slot is taken here, in the same
        synchronous step as the check: the tool runner gathers a turn's calls, so a check here and
        a count after an await would let every parallel call pass `max_per_run` before any counted."""
        # Subscriptions are matched on their subscriber's subject. One every holder of a
        # credential shares names no one, and a notice to it would reach all of them.
        ctx, auth = require_person(
            no_caller=(
                "a notice goes to the signed-in person this run is for, and there is none; nothing was sent"
            ),
            shared_subject=(
                "the credential this run uses names no person (it is shared), so there is no one to "
                "notify; nothing was sent"
            ),
        )
        if not args.title.strip() or len(args.title) > MAX_TITLE_CHARS:
            raise ToolRefused(
                "invalid_title", f"the title must be between 1 and {MAX_TITLE_CHARS} characters"
            )
        if not args.message.strip() or len(args.message) > MAX_MESSAGE_CHARS:
            raise ToolRefused(
                "invalid_message", f"the message must be between 1 and {MAX_MESSAGE_CHARS} characters"
            )
        sent = int(ctx.extras.get(SENT_THIS_RUN_EXTRA) or 0)
        if sent >= self.spec.max_per_run:
            raise ToolRefused(
                "rate_limited",
                f"this run has already sent {self.spec.max_per_run} notice(s), the most one run may; "
                "nothing was sent",
            )
        if reserve:
            ctx.extras[SENT_THIS_RUN_EXTRA] = sent + 1
        return ctx, auth

    async def clean(self, args: _NotifyArgs) -> tuple[str, str]:
        """The title and message as they would be sent: secrets redacted, links removed, screened."""
        from felix.secrets import collected_secret_values, redact_text

        secrets = collected_secret_values(self.settings)
        title = strip_links(redact_text(args.title, secrets))
        body = strip_links(redact_text(args.message, secrets))
        await self.screen(f"{title}\n{body}")
        return title, body

    async def screen(self, text: str) -> None:
        """Screen `text` as every outbound text is (`governance/outbound_screening.py`). A notice
        cannot be quarantined, so a flag refuses it, and so does a screener that could not run:
        unscreened is not clean."""
        from felix.governance.outbound_screening import screen_outbound

        await screen_outbound(
            self.settings,
            self.manifest,
            NOTIFY,
            text,
            flagged="the notice was flagged by content screening and was not sent; nothing was sent",
            unavailable="the notice could not be screened, so it was not sent; nothing was sent",
        )

    @staticmethod
    def own_thread(ctx: RequestContext, call: ToolInvocationCtx | None) -> str:
        """The thread this run is writing to."""
        return (call.thread_id if call is not None else None) or ctx.thread_id or ""

    def thread_for(self, ctx: RequestContext, auth: AuthContext, call: ToolInvocationCtx | None) -> str:
        """The conversation the person can open from the notice. A scheduled task's or a heartbeat's
        firing runs in a thread of its own; the job recorded where it belongs -- the thread a task
        was scheduled from, a heartbeat's own conversation -- and that is the one. Only
        `schedule_task` and a heartbeat's enrolment (`jobs/heartbeat.py:enrol`) write that record
        (`PUT /jobs` strips a caller's), it is used only for a job this person wrote, and only a
        chat thread id of this tenant's (`origin_thread.origin_of`). Otherwise the run's own."""
        return origin_thread.origin_of(ctx, self.tenant_id, auth.acting_for) or self.own_thread(ctx, call)

    async def append(self, thread_id: str, title: str, body: str) -> str:
        """Add the notice to `thread_id` (`notice_entry`); what became of it, an `origin_thread` value.
        Only into a conversation that exists and has no run going on (`idle_append.append_when_idle`)."""
        return await idle_append.append_when_idle(
            self.settings, self.tenant_id, thread_id, [notice_entry(title, body)]
        )

    async def send(self, args: _NotifyArgs, call: ToolInvocationCtx | None) -> str:
        from felix.audit.emit import emit_agent_audit

        ctx, auth = self.admit(args, reserve=True)
        person = auth.acting_for
        allowed = await _rate_limiter(self.settings).hit(
            hourly_key(self.tenant_id, self.manifest_id, person),
            limit=self.spec.max_per_hour,
            window_seconds=HOUR_S,
        )
        if not allowed:
            # A call the hour refuses gives its run slot back: it sent nothing and spent nothing.
            ctx.extras[SENT_THIS_RUN_EXTRA] = max(int(ctx.extras.get(SENT_THIS_RUN_EXTRA) or 0) - 1, 0)
            raise ToolRefused(
                "rate_limited",
                f"this person has been sent {self.spec.max_per_hour} notices from this agent in the last "
                "hour, the most allowed; nothing was sent",
            )
        # The slot `admit` reserved stays spent from here, delivered or not (and screened or not):
        # an undelivered notice still spent its turn, and a run that keeps provoking a refusal must
        # still run out.
        title, body = await self.clean(args)
        thread_id = self.thread_for(ctx, auth, call)
        # The thread first, so the conversation the push links to already holds the notice when
        # the person taps it. Not the run's own thread: a live conversation already shows the call
        # and the model's answer, and its turn owns the leaf.
        thread = origin_thread.NOT_ELIGIBLE
        if thread_id and thread_id != self.own_thread(ctx, call):
            thread = await self.append(thread_id, title, body)
        delivered, push_note = await self.push(
            person, {"kind": "notice", "title": title, "body": body, "thread_id": thread_id}
        )
        result = _outcome(delivered, push_note, thread)
        emit_agent_audit(
            "notify.sent",
            status="ok" if result["channels"] else "undelivered",
            # Never the text: the audit log is read by whoever holds `audit:read`.
            payload={
                "channels": result["channels"],
                "delivered": delivered,
                "thread": thread,
                "thread_id": thread_id,
                "recipient": person,
            },
            manifest_id=self.manifest_id,
        )
        return json.dumps(result)

    async def push(self, person: str, message: dict[str, Any]) -> tuple[int, str | None]:
        """Push `message` to `person`'s browsers: how many accepted it, and why none did."""
        from felix.push.notify import enabled, send_to_person

        if not enabled(self.settings):
            return 0, "push notifications are not configured on this deployment"
        try:
            # A fresh context, as `notify._schedule` uses: the run's carries its manifest's metric
            # allowlist, which would drop `felix_push_delivery`. Awaited: the count is the answer.
            send = send_to_person(self.settings, self.tenant_id, person, message, ttl=NOTICE_TTL_SECONDS)
            delivered = await asyncio.get_running_loop().create_task(send, context=contextvars.Context())
        except Exception:
            logger.warning("notify send failed manifest=%s", self.manifest_id, exc_info=True)
            return 0, "the notice could not be pushed"
        if delivered == 0:
            return 0, "the person has no device subscribed to notifications here, or none accepted it"
        return delivered, None

    async def preview(self, args: ToolInput) -> str:
        """What an approver reads: the notice as it would be sent. Runs `admit` and the cleaning
        first and raises their refusal, so a call the tool would refuse is denied with no approval
        row and no wait (`builder._approval_preview`). Not the hourly limit: the limiter can only
        count a hit, not read one, so a call over the hour can still open a row and is refused when
        it runs after approval."""
        from pydantic import ValidationError

        try:
            parsed = _NotifyArgs.model_validate(args)
        except ValidationError:
            raise ToolRefused("invalid_arguments", "the arguments do not match notify's schema") from None
        _ctx, auth = self.admit(parsed, reserve=False)
        title, body = await self.clean(parsed)
        return f"notify {auth.acting_for} on their own devices\n\n{title}\n\n{body}"


def notice_entry(title: str, body: str) -> AppendableEvent:
    """The entry a notice leaves in the conversation it links to: for the person, not the model.

    `custom`, so `include_in_llm_context` keeps it out of every prompt (`in_context` is spelled out
    false, for a reader); `role` says who it is from. Plain text: a client draws a custom entry's
    content as written, not as markdown."""
    from felix.session.types import AppendableEvent

    return AppendableEvent(
        kind="custom",
        role="assistant",
        content=f"{title}\n\n{body}",
        metadata={"type": NOTICE_ENTRY_TYPE, "title": title, "in_context": False},
    )


def _outcome(delivered: int, push_note: str | None, thread: str) -> dict[str, Any]:
    """The result the model reads: how many browsers took it, the channels that delivered it, what
    became of it in the conversation it links to, and a note saying why anything did not."""
    appended = thread == idle_append.APPENDED
    channels = [*(["push"] if delivered else []), *(["thread"] if appended else [])]
    notes = [n for n in (push_note, _THREAD_NOTES.get(thread)) if n]
    if appended and push_note:
        notes.append("it is in their conversation for when they next open it")
    elif not channels:
        notes.append("the person was not notified")
    result: dict[str, Any] = {"delivered": delivered, "channels": channels, "thread": thread}
    if notes:
        result["note"] = "; ".join(notes)
    return result


def make_notify_tool(settings: Any, *, tenant_id: str, manifest: Manifest) -> Tool:
    """`notify` for one manifest (which must carry `spec.notify`) in one tenant."""
    spec = manifest.spec.notify
    if spec is None:
        raise ValueError("make_notify_tool needs a manifest with spec.notify")
    notices = _Notices(settings, tenant_id=tenant_id, manifest=manifest, spec=spec)

    async def _notify(args: _NotifyArgs, ctx: ToolInvocationCtx | None = None) -> str:
        try:
            return await notices.send(args, ctx)
        except ToolRefused as refused:
            return refused.result()

    tool = define_tool(
        name=NOTIFY,
        description=(
            "Send a short plain-text notice to the person you are working for, on the devices where "
            "they turned on notifications. Use it when something they asked about has happened or "
            "needs them -- especially from a scheduled task, where nobody is reading the "
            "conversation. It goes to that person only; there is no recipient to choose. Links are "
            f"removed. At most {spec.max_per_run} per run. From a scheduled task it is also added to "
            "the conversation the task was scheduled from, for the person to read there. Returns how "
            "many devices received it and the channels that delivered it."
        ),
        args=_NotifyArgs,
        handler=_notify,
    )
    tool.approval_preview = notices.preview
    return tool


__all__ = [
    "LINK_REMOVED",
    "NOTICE_ENTRY_TYPE",
    "NOTIFY",
    "hourly_key",
    "link_host",
    "make_notify_tool",
    "notice_entry",
    "reset_notify_for_tests",
    "strip_links",
]
