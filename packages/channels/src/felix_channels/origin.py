"""Where a run's answer goes: the `origin` this plugin tags its runs with, written and read here.

`start_durable_chat(origin=...)` keeps it in the run's state; core hands it back to this plugin's
`register_run_finished` hook when the run ends, and only for runs tagged with `PLUGIN`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PLUGIN = "felix-channels"


@dataclass(frozen=True, slots=True)
class ReplyTo:
    """A Slack conversation: the channel entry, the Slack channel, and the thread (None for a DM's
    main line, answered in place)."""

    channel: str
    conversation: str
    thread_ts: str | None

    def to_origin(self) -> dict[str, str]:
        return {
            "plugin": PLUGIN,
            "channel": self.channel,
            "conversation": self.conversation,
            "thread_ts": self.thread_ts or "",
        }

    @classmethod
    def from_origin(cls, origin: Mapping[str, str]) -> ReplyTo:
        return cls(
            channel=str(origin.get("channel") or ""),
            conversation=str(origin.get("conversation") or ""),
            thread_ts=str(origin.get("thread_ts") or "") or None,
        )


__all__ = ["PLUGIN", "ReplyTo"]
