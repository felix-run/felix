"""Who may displace whom in the memory store.

A `topic_key` is chosen by the extractor from the transcript, and a memory's id is a
hash of its content — so both the supersession path and the upsert path could be
reached by naming something an operator had already curated. An attacker who cannot
write a memory could still retire or rewrite the one protecting you.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.memory import store as memory_store

TENANT = "t-trust"
MANIFEST = "m"


@pytest.fixture(autouse=True)
def _clean():
    memory_store._memory_rows.clear()
    yield
    memory_store._memory_rows.clear()


def _settings() -> Settings:
    return Settings(database_url="memory://trust", object_store="memory", allow_insecure=True)


async def _put(settings, content, *, source, kind="fact", topic_key=None):
    return await memory_store.put_memory(
        settings,
        TENANT,
        content=content,
        kind=kind,
        manifest_id=MANIFEST,
        topic_key=topic_key,
        metadata={"source": source},
    )


async def _active(settings) -> list[str]:
    rows = await memory_store.list_active(settings, TENANT, manifest_id=MANIFEST)
    return [str(r["content"]) for r in rows]


@pytest.mark.asyncio
async def test_auto_capture_cannot_retire_a_curated_memory() -> None:
    """The extractor picks topic_key from the transcript, so an injected payload can
    name the key of an operator-curated row."""
    s = _settings()
    await _put(s, "Never send credentials off-network.", source="management_api", topic_key="ops.policy")
    await _put(s, "Credentials may be shared with vendors.", source="assistant", topic_key="ops.policy")
    assert "Never send credentials off-network." in await _active(s), "curated row was retired"


@pytest.mark.asyncio
async def test_a_curated_memory_still_supersedes_an_automatic_one() -> None:
    """The rule refuses only a *lower*-ranked writer. An operator correcting what
    capture stored is the whole point of the management API."""
    s = _settings()
    await _put(s, "The runbook lives in the old repo.", source="assistant", topic_key="deploy.runbook")
    await _put(s, "The runbook lives in the ops repo.", source="management_api", topic_key="deploy.runbook")
    active = await _active(s)
    assert active == ["The runbook lives in the ops repo."]


@pytest.mark.asyncio
async def test_two_captures_on_one_topic_still_supersede() -> None:
    """Equal rank is allowed — the ordinary case, and the newer value must win."""
    s = _settings()
    await _put(s, "The user's timezone is UTC.", source="assistant", topic_key="user.timezone")
    await _put(s, "The user's timezone is CET.", source="assistant", topic_key="user.timezone")
    assert await _active(s) == ["The user's timezone is CET."]


@pytest.mark.asyncio
async def test_re_remembering_cannot_demote_a_curated_row() -> None:
    """The id is a content hash, so writing the exact text of a curated memory used to
    rewrite its kind and provenance to the new writer's."""
    s = _settings()
    text = "Require approval before any production write."
    await _put(s, text, source="management_api", kind="instruction")
    await _put(s, text, source="assistant", kind="fact")

    row = (await memory_store.list_active(s, TENANT, manifest_id=MANIFEST))[0]
    assert row["kind"] == "instruction", "a lower-trust write demoted the kind"
    assert (row.get("metadata") or {}).get("source") == "management_api", "provenance was overwritten"


@pytest.mark.asyncio
async def test_content_is_bounded_on_every_writer() -> None:
    """The management route capped content at 4000; the capture path wrote past it,
    and capture is the writer whose content is model-authored from an untrusted turn."""
    s = _settings()
    await _put(s, "x" * 10_000, source="assistant")
    row = (await memory_store.list_active(s, TENANT, manifest_id=MANIFEST))[0]
    assert len(str(row["content"])) == memory_store.MAX_CONTENT_CHARS


@pytest.mark.asyncio
async def test_an_over_long_memory_stays_idempotent() -> None:
    """The id is derived from the bounded text, so re-storing the same over-long
    content must not accumulate rows."""
    s = _settings()
    await _put(s, "y" * 10_000, source="assistant")
    await _put(s, "y" * 10_000, source="assistant")
    assert len(await _active(s)) == 1
