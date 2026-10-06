"""The copy rule: an agent's save carrying an imported file's text carries the import's lineage.

A file matches by its bytes or by its normalized text (`library.normalized_text`: NFKC,
casefolded, whitespace collapsed), so a copy that only re-spaces, re-cases or swaps Unicode
compatibility forms is still a copy. A paraphrase is not caught. A file under
`library.COPY_FLOOR_CHARS` is never looked up, so boilerplate cannot taint every save. The stores
are the `memory://` twins.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.library_store import ImportOrigin, get_skill_library_store
from felix.storage import MemoryObjectStore

from tests.skill_import_fake import skill_md

PLAYBOOK = (
    "# Refund playbook\n\n"
    "Refund an order in full when it arrived damaged.\n"
    "Escalate anything over 500 to the finance queue.\n"
)
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"pixels" * 20).decode()


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://skill-copy")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


async def _import(settings: Settings, store: MemoryObjectStore, extra: dict[str, str]) -> None:
    origin = ImportOrigin(
        source="github:acme/skills/skills/refunds", ref="main", commit="a" * 40, tree_hash="t"
    )
    await library.save_draft(
        settings,
        "acme",
        files={"SKILL.md": skill_md("refunds", "Issue refunds.").decode(), **extra},
        provenance=library.DraftProvenance(source="import", author="ops", origin=origin),
        object_store=store,
    )


async def _agent_saves(settings: Settings, store: MemoryObjectStore, extra: dict[str, str]) -> dict[str, Any]:
    """An agent's new skill, under another name, carrying ``extra`` beside its own SKILL.md."""
    return await library.save_draft(
        settings,
        "acme",
        files={"SKILL.md": skill_md("refund-notes", "Our own notes on refunds.").decode(), **extra},
        provenance=library.DraftProvenance(source="agent", author="c", origin_manifest_id="contributor"),
        object_store=store,
    )


def test_normalized_text_folds_compatibility_forms_case_and_whitespace() -> None:
    assert library.normalized_text("  Ｒｅｆｕｎｄ the\tOFFICE\n\n ﬁle  ") == "refund the office file"
    assert library.normalized_text("Straße") == library.normalized_text("STRASSE")
    assert library.normalized_text(" \n\t ") == ""


@pytest.mark.parametrize(
    "variant",
    [
        PLAYBOOK,
        PLAYBOOK.replace("\n", "\r\n").replace(" ", "   "),
        "   " + PLAYBOOK.replace("\n\n", "\n\n\n\n") + "\n\n",
        PLAYBOOK.upper(),
        PLAYBOOK.replace("Refund", "Ｒｅｆｕｎｄ").replace(" ", " "),
        PLAYBOOK.replace("finance", "ﬁnance"),
    ],
    ids=["bytes", "whitespace", "blank-lines", "case", "full-width-and-nbsp", "ligature"],
)
async def test_a_copy_that_differs_only_in_form_carries_the_lineage(
    variant: str, settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store, {"references/playbook.md": PLAYBOOK})
    saved = await _agent_saves(settings, store, {"references/playbook.md": variant})
    assert saved["lineage_import"] is True


async def test_a_paraphrase_is_not_caught(settings: Settings, store: MemoryObjectStore) -> None:
    """The documented limit: the rule compares text, not meaning."""
    await _import(settings, store, {"references/playbook.md": PLAYBOOK})
    paraphrase = (
        "# Refunds\n\nWhen an order shows up damaged, give the whole amount back.\n"
        "Send anything above 500 to finance.\n"
    )
    saved = await _agent_saves(settings, store, {"references/playbook.md": paraphrase})
    assert saved["lineage_import"] is False


@pytest.mark.parametrize(
    "short",
    ["", "\n", "MIT\n", "[]\n", "# Notes\n", "Apache-2.0 licensed text here.\n"],
    ids=["empty", "newline", "license-id", "json-list", "heading", "one-short-line"],
)
async def test_a_short_or_empty_file_never_taints(
    short: str, settings: Settings, store: MemoryObjectStore
) -> None:
    assert len(library.normalized_text(short)) < library.COPY_FLOOR_CHARS
    await _import(settings, store, {"references/short.md": short, "references/playbook.md": PLAYBOOK})
    saved = await _agent_saves(settings, store, {"references/short.md": short})
    assert saved["lineage_import"] is False


async def test_at_the_floor_a_file_is_looked_up(settings: Settings, store: MemoryObjectStore) -> None:
    exact = "x" * library.COPY_FLOOR_CHARS
    await _import(settings, store, {"references/edge.md": exact})
    assert (await _agent_saves(settings, store, {"references/edge.md": exact.upper()}))[
        "lineage_import"
    ] is True


async def test_a_binary_asset_is_matched_by_its_bytes_only(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store, {"assets/logo.png": PNG})
    rows = await get_skill_library_store(settings).list_files("acme", "refunds", "0.1.0")
    by_path = {r["path"]: r for r in rows}
    assert by_path["assets/logo.png"]["normalized_sha256"] is None
    assert by_path["SKILL.md"]["normalized_sha256"] is not None
    assert (await _agent_saves(settings, store, {"assets/logo.png": PNG}))["lineage_import"] is True


async def test_a_file_saved_before_the_normalized_digest_is_matched_by_its_bytes(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """A row from before migration 0032 has no normalized digest: its bytes still match, and a
    re-spaced copy of it is not caught until the import is saved again."""
    import hashlib

    lib = get_skill_library_store(settings)
    data = PLAYBOOK.encode()
    row = {
        "name": "refunds",
        "version": "0.1.0",
        "status": "draft",
        "source": "import",
        "author": "ops",
        "security_status": "pass",
        "created_at": 1,
        "lineage_import": True,
    }
    files = [
        {"path": "references/playbook.md", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    ]
    await lib.insert_version("acme", row, files, created_by="ops", at=1)
    assert (await lib.list_files("acme", "refunds", "0.1.0"))[0]["normalized_sha256"] is None

    respaced = await _agent_saves(settings, store, {"references/playbook.md": PLAYBOOK.upper()})
    assert respaced["lineage_import"] is False, "the documented gap until the import is re-saved"
    copied = await _agent_saves(settings, store, {"references/playbook.md": PLAYBOOK})
    assert copied["lineage_import"] is True
