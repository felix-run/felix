"""`post`: an agent publishing to a shared place the operator approved in advance.

Bound by `manifests/builder.py` for a manifest with `spec.destinations`, before the governance
stack, so every wrapper applies -- and with `approval: true` an approval rule
(`permission_mode.POST_APPROVAL`) holds each call until a person has read it, which no permission
mode waives. Off by default: the runs a post is for -- a job's firing, a heartbeat -- have nobody
watching, and the operator registering the destination (`FELIX_DESTINATIONS`) was the approval.

Who may make one: a run nobody is watching -- a job's firing, a heartbeat, a trigger, and what
they start (`RequestContext.unattended`). A call from a person's chat is refused (`attended`)
unless the manifest's `attended: approval` holds it for that person's approval instead
(`attended_approval_rule`, unwaivable); a call with no request context is refused outright; and a
manifest that admits anonymous callers does not compile (`builder._bind_destinations`).

The destination is one of the manifest's ids, never a URL or a channel. What reaches it is text a
model wrote, possibly under a prompt injection, so before anything leaves: known secret values are
redacted (the governance stack masks a tool's *output*; a post is its *arguments*), links are
handled as the destination's registration says (`Destination.apply_links`: removed by default,
kept only when the operator chose to), and the result is screened as a notice is
(`governance/outbound_text.py:prepare_outbound`, the step `notify` takes too), a hit refusing the
call.

Each post names an `edition` -- one stable name per logical post, `brief:2026-10-29` -- and the
ledger (`destinations/ledger.py`) holds one row per tenant, destination, manifest and edition. The
call reads the row before it cleans or screens anything (and so does the approval preview, so no
approval is asked for an edition already there), then claims it before it delivers, so of two calls
naming one edition (a retry, a resumed run, the job's next firing) exactly one delivers:

* already `posted`: the call answers `already_posted` with the provider's ref -- a success the
  model reads, not an error;
* `posting` and recent: refused with `in_flight` -- another call is delivering it now;
* `posting` and old, or `maybe_posted`: refused with `maybe_posted`. The earlier delivery's
  outcome is unknown, so the harness does not guess; a person checks the destination;
* claimed: delivered by the destination's kind (`destinations/__init__.py`), and the row is
  settled `posted`, released (it certainly did not land, so the edition may be tried again), or
  settled `maybe_posted`.

`max_per_run` is a counter on the request context, reserved before anything awaits so a turn's
parallel calls cannot all pass it, and spent whatever the call's outcome -- except an
`already_posted`, which posted nothing.

Every refusal is a result the model reads, `{"error": <code>, "detail": <message>}`, written here
and never an exception's text; a kind's own `detail` is cut to `MAX_DETAIL_CHARS`. Every call
that reached a destination, or found its edition there, is audited as `post.sent` with the
destination, edition, status and ref -- never the text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from felix import destinations as _registry
from felix.destinations import (
    MAX_DETAIL_CHARS,
    Delivery,
    Destination,
    EditionKey,
    Post,
    destination_kind,
    ledger,
)
from felix.governance.permission_mode import POST_ATTENDED_APPROVAL_RULE_ID, POST_TOOL_NAME
from felix.logging_setup import loggable
from felix.tools.refusal import ToolRefused
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

if TYPE_CHECKING:
    from felix.context import RequestContext
    from felix.manifests.schema import ApprovalRule, DestinationsSpec, Manifest

logger = logging.getLogger("felix.destinations.tool")

POST = POST_TOOL_NAME
MAX_TEXT_CHARS = 8000
MAX_TITLE_CHARS = 200
EDITION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$"
# `\Z`, not `$`, which also matches before a trailing newline.
_EDITION_RE = re.compile(EDITION_PATTERN.removesuffix("$") + r"\Z")
# The run's count of posts made, on `RequestContext.extras`.
POSTED_THIS_RUN_EXTRA = "post.posted_this_run"


class _PostArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each refused by the handler with a message for the model, and advertised here.
    destination: str
    edition: str = Field(
        description=(
            "A stable name for this one logical post, such as `brief:2026-10-29` or "
            "`release:1.4.0`: the same post always gets the same edition, so a retry or the next "
            "run cannot post it twice. An edition already posted to this destination is not posted "
            "again. Letters, digits and `:._-`, up to 128 characters."
        ),
        json_schema_extra={"pattern": EDITION_PATTERN, "maxLength": 128},
    )
    text: str = Field(
        description=(
            "What to post, as plain text or the destination's markup. Whether links survive is the "
            "destination's setting: some keep them, some keep only certain hosts', some remove them."
        ),
        json_schema_extra={"minLength": 1, "maxLength": MAX_TEXT_CHARS},
    )
    title: str = Field(
        default="",
        description="An optional headline for the post.",
        json_schema_extra={"maxLength": MAX_TITLE_CHARS},
    )


def _args_model(ids: list[str]) -> type[_PostArgs]:
    """`_PostArgs` with `destination` advertised as the manifest's ids."""
    return create_model(
        "PostArgs",
        __base__=_PostArgs,
        destination=(
            str,
            Field(
                description="Where to post: one of the destinations this agent may post to.",
                json_schema_extra={"enum": list(ids)},
            ),
        ),
    )


def valid_edition(edition: str) -> bool:
    return bool(_EDITION_RE.match(edition))


class _Posts:
    """One manifest's posts, bound to its tenant and `spec.destinations`. Every refusal raises
    `ToolRefused`; the tool wrapper in `make_post_tool` turns it into the result."""

    def __init__(
        self,
        settings: Any,
        *,
        tenant_id: str,
        manifest: Manifest,
        spec: DestinationsSpec,
        destinations: dict[str, Destination],
    ) -> None:
        self.settings, self.tenant_id, self.manifest, self.spec = settings, tenant_id, manifest, spec
        self.destinations = destinations
        self.manifest_id = manifest.metadata.name

    def admit(self, args: _PostArgs, *, reserve: bool) -> RequestContext:
        """The checks a call makes before anything awaits, run by the call and, first, by its
        approval preview. With ``reserve`` the run's slot is taken in the same synchronous step."""
        from felix.context import try_get_context

        ctx = try_get_context()
        if ctx is None:
            # Nothing to say who is posting, and no run to count against: closed, not open.
            raise ToolRefused("no_context", "this call is not part of a run, so nothing was posted")
        if not ctx.unattended and self.spec.attended == "refuse":
            raise ToolRefused(
                "attended",
                "this agent posts to shared destinations only from scheduled or triggered runs, not "
                "from a conversation; nothing was posted",
            )
        if args.destination not in self.destinations:
            raise ToolRefused(
                "unknown_destination",
                f"{args.destination!r} is not a destination this agent may post to; it may post to "
                f"{', '.join(sorted(self.destinations))}. Nothing was posted",
            )
        if not valid_edition(args.edition):
            raise ToolRefused(
                "invalid_edition",
                "the edition must be 1-128 letters, digits or `:._-`, starting with a letter or digit; "
                "nothing was posted",
            )
        if not args.text.strip() or len(args.text) > MAX_TEXT_CHARS:
            raise ToolRefused("invalid_text", f"the text must be between 1 and {MAX_TEXT_CHARS} characters")
        if len(args.title) > MAX_TITLE_CHARS:
            raise ToolRefused("invalid_title", f"the title must be at most {MAX_TITLE_CHARS} characters")
        made = int(ctx.extras.get(POSTED_THIS_RUN_EXTRA) or 0)
        if made >= self.spec.max_per_run:
            raise ToolRefused(
                "rate_limited",
                f"this run has already made {self.spec.max_per_run} post(s), the most one run may; "
                "nothing was posted",
            )
        if reserve:
            ctx.extras[POSTED_THIS_RUN_EXTRA] = made + 1
        return ctx

    @staticmethod
    def _give_back(ctx: RequestContext) -> None:
        ctx.extras[POSTED_THIS_RUN_EXTRA] = max(int(ctx.extras.get(POSTED_THIS_RUN_EXTRA) or 0) - 1, 0)

    def key(self, args: _PostArgs) -> EditionKey:
        """The ledger's key for this call's edition: built once per call, used for every step."""
        return EditionKey(self.tenant_id, args.destination, self.manifest_id, args.edition)

    async def _register_kind_secrets(self, dest: Destination) -> None:
        """Mask the values the destination's kind sends with (`DestinationSecrets`) before the text
        is cleaned, so they are redacted from it on the first post in a process too."""
        from felix.destinations import destination_secrets
        from felix.secrets import register_resolved_secret

        adapter = destination_kind(dest.kind)
        try:
            values = await destination_secrets(adapter, self.settings, self.tenant_id, dest)
        except Exception:
            logger.warning("destination %s (%s) secrets unavailable", dest.name, dest.kind, exc_info=True)
            raise ToolRefused(
                "unavailable", "the destination's credentials could not be prepared; nothing was posted"
            ) from None
        for value in values:
            register_resolved_secret(value)

    async def clean(self, args: _PostArgs) -> tuple[str, str]:
        """The title and text as they would be posted: secrets redacted -- the destination kind's own
        first registered (`_register_kind_secrets`) -- links as the destination says, screened
        (`governance/outbound_text.py:prepare_outbound`)."""
        from felix.governance.outbound_text import prepare_outbound

        await self._register_kind_secrets(self.destinations[args.destination])

        title, text = await prepare_outbound(
            self.settings,
            self.manifest,
            POST,
            [args.title, args.text],
            links=self.destinations[args.destination].apply_links,
            flagged="the post was flagged by content screening and was not posted; nothing was posted",
            unavailable="the post could not be screened, so it was not posted; nothing was posted",
        )
        return title, text

    def _audit(self, args: _PostArgs, status: str, ref: str | None) -> None:
        from felix.audit.emit import emit_agent_audit

        emit_agent_audit(
            "post.sent",
            status=status,
            # Never the text: the audit log is read by whoever holds `audit:read`.
            payload={"destination": args.destination, "edition": args.edition, "status": status, "ref": ref},
            manifest_id=self.manifest_id,
        )

    def _ledger_unavailable(self, args: _PostArgs, what: str) -> ToolRefused:
        logger.warning(
            "post ledger %s failed destination=%s", what, loggable(args.destination, limit=64), exc_info=True
        )
        return ToolRefused(
            "unavailable", "whether this edition was already posted could not be checked; nothing was posted"
        )

    async def _existing(self, args: _PostArgs, key: EditionKey) -> dict[str, Any] | None:
        try:
            return await ledger.get(self.settings, key)
        except Exception:
            raise self._ledger_unavailable(args, "read") from None

    async def send(self, args: _PostArgs, call: ToolInvocationCtx | None) -> str:
        ctx = self.admit(args, reserve=True)
        # The slot stays spent from here whatever happens -- a screened, refused or undelivered post
        # still spent its turn -- except for an edition already posted, which posted nothing.
        # The ledger first, so an edition already there is answered before any screening is paid
        # for. The claim below is still what decides: this read only saves work.
        key = self.key(args)
        existing = await self._existing(args, key)
        if existing is not None:
            return self._already(args, existing, ctx)
        title, text = await self.clean(args)
        thread_id = (call.thread_id if call is not None else None) or ctx.thread_id or ""
        try:
            claim = await ledger.claim(self.settings, key, thread_id=thread_id)
        except Exception:
            raise self._ledger_unavailable(args, "claim") from None
        if not claim.won:
            return self._already(args, claim.row, ctx)
        post = Post(
            edition=args.edition,
            text=text,
            title=title,
            manifest_id=self.manifest_id,
            key=key.post_key,
            unattended=ctx.unattended,
        )
        delivery = await self._deliver(self.destinations[args.destination], post)
        await self._settle(args, key, delivery.status, delivery.ref)
        self._audit(args, "maybe_posted" if delivery.status == "unknown" else delivery.status, delivery.ref)
        if delivery.status == "posted":
            return json.dumps(
                {
                    "status": "posted",
                    "destination": args.destination,
                    "edition": args.edition,
                    "ref": delivery.ref,
                }
            )
        # The kind's own words, cut: a plugin's detail is written for the model, but not unbounded.
        detail = str(delivery.detail or "")[:MAX_DETAIL_CHARS]
        if delivery.status == "not_posted":
            raise ToolRefused(
                "not_posted",
                f"{detail or 'the destination did not take the post'}; nothing was posted, and "
                "the same edition may be tried again",
            )
        raise ToolRefused(
            "maybe_posted",
            f"{detail or 'the delivery did not finish'}. It may have been posted, so do not post "
            "this edition again, under it or another name; a person must check the destination",
        )

    async def _settle(self, args: _PostArgs, key: EditionKey, status: str, ref: str | None) -> None:
        """Record the delivery's outcome on the claimed row. A write that fails leaves it `posting`,
        which every later call reads as `in_flight` and then `maybe_posted` -- the safe side of not
        knowing."""
        try:
            if status == "not_posted":
                await ledger.release(self.settings, key)
            else:
                settled = ledger.POSTED if status == "posted" else ledger.MAYBE_POSTED
                await ledger.settle(self.settings, key, status=settled, ref=ref)
        except Exception:
            logger.warning(
                "post ledger settle failed destination=%s status=%s",
                loggable(args.destination, limit=64),
                status,
                exc_info=True,
            )

    async def _deliver(self, dest: Destination, post: Post) -> Delivery:
        adapter = destination_kind(dest.kind)
        if adapter is None:
            # Uninstalled since the compile; nothing was sent.
            return Delivery("not_posted", detail="the destination's kind is no longer installed")
        try:
            # Read at call time, so the bound is the registry's (and a test's) current one.
            async with asyncio.timeout(_registry.DELIVERY_BUDGET_S):
                delivery = await adapter.deliver(self.settings, self.tenant_id, dest, post)
        except TimeoutError:
            # Past the budget the harness stops waiting; whatever the kind sent may have landed.
            logger.warning("destination %s (%s) delivery outlived its budget", dest.name, dest.kind)
            return Delivery(
                "unknown", detail="the destination's delivery took too long, so it may have been posted"
            )
        except Exception:
            # An adapter that raised may have sent first: unknown, never a guess that it did not.
            logger.warning("destination %s (%s) delivery raised", dest.name, dest.kind, exc_info=True)
            return Delivery("unknown", detail="the destination's delivery failed partway")
        if getattr(delivery, "status", None) not in ("posted", "not_posted", "unknown"):
            logger.warning("destination %s (%s) answered no delivery status", dest.name, dest.kind)
            return Delivery("unknown", detail="the destination's delivery gave no outcome")
        return delivery

    def _refusal_for(self, args: _PostArgs, row: dict[str, Any]) -> ToolRefused:
        """The refusal for an edition the ledger holds that is not `posted`."""
        if ledger.in_flight(row):
            return ToolRefused(
                "in_flight",
                f"another call is posting edition {args.edition!r} to {args.destination} right now; do not "
                "post it again, under this edition or another",
            )
        return ToolRefused(
            "maybe_posted",
            f"edition {args.edition!r} was already sent to {args.destination} and whether it landed is not "
            "known. Do not post it again, under this edition or another; a person must check the destination",
        )

    def _already(self, args: _PostArgs, row: dict[str, Any], ctx: RequestContext) -> str:
        if row.get("status") == ledger.POSTED:
            self._give_back(ctx)
            self._audit(args, "already_posted", row.get("ref"))
            return json.dumps(
                {
                    "status": "already_posted",
                    "destination": args.destination,
                    "edition": args.edition,
                    "ref": row.get("ref"),
                }
            )
        refusal = self._refusal_for(args, row)
        self._audit(args, refusal.code, None)
        raise refusal

    async def preview(self, args: ToolInput) -> str:
        """What an approver reads: the post as it would be made. Runs `admit`, the ledger read and
        the cleaning first and raises their refusal, so a call the tool would refuse -- an edition
        already posted included -- opens no approval row."""
        from pydantic import ValidationError

        try:
            parsed = _PostArgs.model_validate(args)
        except ValidationError:
            raise ToolRefused("invalid_arguments", "the arguments do not match post's schema") from None
        self.admit(parsed, reserve=False)
        existing = await self._existing(parsed, self.key(parsed))
        if existing is not None:
            if existing.get("status") == ledger.POSTED:
                raise ToolRefused(
                    "already_posted",
                    f"edition {parsed.edition!r} is already posted to {parsed.destination}; "
                    "nothing to approve",
                )
            raise self._refusal_for(parsed, existing)
        title, text = await self.clean(parsed)
        head = f"post to {parsed.destination} (edition {parsed.edition})"
        return f"{head}\n\n{title}\n\n{text}" if title else f"{head}\n\n{text}"


def attended_approval_rule(spec: DestinationsSpec, approvals: list[ApprovalRule]) -> ApprovalRule | None:
    """The rule `attended: approval` adds over `post`, or None: one-shot and bound to the person,
    passed to `apply_approvals` as conditional on the call (`_attended`), and unwaivable
    (`permission_mode.POST_ATTENDED_APPROVAL_RULE_ID`). None under `refuse`, and when `approval` is on,
    whose rule already holds every call. `ValueError` when a manifest rule matches `post`: a rule is
    selected per tool, so ours would replace it for unattended calls or it would replace ours for
    attended ones, and neither is what the manifest says."""
    if spec.attended != "approval" or spec.approval:
        return None
    from felix.manifests.schema import ApprovalRule
    from felix.manifests.tool_match import matches_any

    if any(matches_any(r.tools, POST) for r in approvals):
        raise ValueError(
            "spec.destinations.attended: approval cannot be combined with a spec.approvals rule matching "
            "'post'; set spec.destinations.approval: true to hold every post, or drop the rule"
        )
    return ApprovalRule(
        id=POST_ATTENDED_APPROVAL_RULE_ID,
        description="Approve this post from a conversation",
        tools=[POST],
        one_shot=True,
        bind_principal=True,
    )


async def _attended(_args: ToolInput) -> bool:
    """Whether a call needs `attended: approval`'s rule: someone is watching the run. No context
    gates the call (the tool then refuses it anyway)."""
    from felix.context import try_get_context

    ctx = try_get_context()
    return ctx is None or not ctx.unattended


def make_post_tool(
    settings: Any, *, tenant_id: str, manifest: Manifest, destinations: dict[str, Destination]
) -> Tool:
    """`post` for one manifest (which must carry `spec.destinations`) in one tenant, over the
    destinations `resolve_destinations` already checked for it."""
    spec = manifest.spec.destinations
    if spec is None:
        raise ValueError("make_post_tool needs a manifest with spec.destinations")
    posts = _Posts(settings, tenant_id=tenant_id, manifest=manifest, spec=spec, destinations=destinations)
    args_model = _args_model(spec.ids)

    async def _post(args: _PostArgs, ctx: ToolInvocationCtx | None = None) -> str:
        try:
            return await posts.send(args, ctx)
        except ToolRefused as refused:
            return refused.result()

    where = (
        "scheduled or triggered runs"
        if spec.attended == "refuse"
        else "any run (from a conversation, with the person's approval)"
    )
    tool = define_tool(
        name=POST,
        description=(
            "Publish a post to a shared place the operator set up for this agent -- a team channel, a "
            f"feed: {', '.join(spec.ids)}. Use it for what belongs with a team rather than one person, "
            f"such as a scheduled brief; it works from {where}. Name each post with a stable `edition` "
            "(`brief:2026-10-29`), the same every time for the same post: an edition already posted is "
            "not posted again, and the call answers `already_posted`. If it answers `maybe_posted` or "
            f"`in_flight`, do not post again under any edition. At most {spec.max_per_run} per run. "
            "Returns the status and the destination's id for the post."
        ),
        args=args_model,
        handler=_post,
    )
    tool.approval_preview = posts.preview
    tool.approval_condition = _attended
    return tool


__all__ = [
    "EDITION_PATTERN",
    "MAX_TEXT_CHARS",
    "POST",
    "POSTED_THIS_RUN_EXTRA",
    "attended_approval_rule",
    "make_post_tool",
    "valid_edition",
]
