"""The wire contract — the OpenAPI document and the SSE event vocabulary — matches its snapshots.

`schemas/openapi.json` and `schemas/sse-events.json` are checked in so a change to either surface
arrives as a diff someone reads. felix-run/web mirrors the event names by hand with an open arm in
its union, so an added or renamed frame used to do nothing on either side, silently. Regenerate
with `make contract` and review what changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.scripts_loader import load_script

contract = load_script("gen-wire-contract")


def test_the_sse_event_snapshot_is_current() -> None:
    stored = json.loads(contract.SSE_EVENTS.read_text(encoding="utf-8"))
    assert stored == contract.build_events(), "run `make contract` and review the diff"


def test_the_openapi_snapshot_is_current() -> None:
    stored = json.loads(contract.OPENAPI.read_text(encoding="utf-8"))
    assert stored == contract.build_openapi(), "run `make contract` and review the diff"


def test_the_vocabulary_holds_the_frames_a_client_depends_on() -> None:
    """Guards the scan: one that found nothing would agree with an empty snapshot."""
    events = set(contract.build_events()["events"])
    assert {"text_delta", "tool_start", "tool_end", "done", "approval_required", "run_accepted"} <= events


def _scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> Any:
    (tmp_path / "producer.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(contract, "SOURCE_ROOTS", [tmp_path])
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    return contract.scan()


def test_a_producer_whose_name_is_not_a_literal_fails_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names, unexplained = _scan(tmp_path, monkeypatch, "name = 'x'\nEvent(event=name, data={})\n")
    assert names == set() and unexplained == ["producer.py: name"]


def test_a_side_event_is_read_off_its_emit_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = (
        "from felix.side_events import emit\nasync def f():\n    await emit('t', 'brand_new_frame', {})\n"
    )
    names, unexplained = _scan(tmp_path, monkeypatch, source)
    assert names == {"brand_new_frame"} and unexplained == []
