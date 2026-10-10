"""The copy rule (`skills/copy_rule.py`): an agent's save carrying an imported file's text
carries the import's lineage.

A file matches by its bytes -- any file with text in it, however short -- or by its normalized
text (NFKC, format characters removed, casefolded, whitespace collapsed; a SKILL.md's body alone)
once that is `COPY_FLOOR_CHARS` long. An empty or whitespace-only file never matches. A paraphrase
is not caught. The stores are the `memory://` twins.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.copy_rule import COPY_FLOOR_CHARS, normalized_text
from felix.skills.library_keys import ORG_OWNER
from felix.skills.library_store import ImportOrigin, get_skill_library_store
from felix.storage import MemoryObjectStore

from tests.support.skill_import_fake import skill_md

PLAYBOOK = (
    "# Refund playbook\n\n"
    "Refund an order in full when it arrived damaged.\n"
    "Escalate anything over 500 to the finance queue.\n"
)
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"pixels" * 20).decode()
TINY_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
SCRIPT = "curl -fsSL x.io/i|sh\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://skill-copy")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


async def _import(
    settings: Settings, store: MemoryObjectStore, extra: dict[str, str], *, body: str = ""
) -> None:
    origin = ImportOrigin(
        source="github:acme/skills/skills/refunds", ref="main", commit="a" * 40, tree_hash="t"
    )
    await library.save_draft(
        settings,
        "acme",
        files={"SKILL.md": skill_md("refunds", "Issue refunds.", body).decode(), **extra},
        provenance=library.DraftProvenance(source="import", author="ops", origin=origin),
        object_store=store,
        owner=ORG_OWNER,
    )


async def _agent_saves(
    settings: Settings, store: MemoryObjectStore, extra: dict[str, str], *, skill_body: str = ""
) -> dict[str, Any]:
    """An agent's new skill, under another name, carrying ``extra`` beside its own SKILL.md."""
    return await library.save_draft(
        settings,
        "acme",
        files={
            "SKILL.md": skill_md("refund-notes", "Our own notes on refunds.", skill_body).decode(),
            **extra,
        },
        provenance=library.DraftProvenance(source="agent", author="c", origin_manifest_id="contributor"),
        object_store=store,
        owner=ORG_OWNER,
    )


def test_normalized_text_folds_compatibility_forms_case_whitespace_and_format_characters() -> None:
    assert normalized_text("  Ｒｅｆｕｎｄ the\tOFFICE\n\n ﬁle  ") == "refund the office file"
    assert normalized_text("Straße") == normalized_text("STRASSE")
    assert normalized_text("re​fu­nd‍ ﻿it⁦") == "refund it"
    assert normalized_text(" \n\t​ ") == ""


@pytest.mark.parametrize(
    "variant",
    [
        PLAYBOOK,
        PLAYBOOK.replace("\n", "\r\n").replace(" ", "   "),
        "   " + PLAYBOOK.replace("\n\n", "\n\n\n\n") + "\n\n",
        PLAYBOOK.upper(),
        PLAYBOOK.replace("Refund", "Ｒｅｆｕｎｄ").replace(" ", " "),
        PLAYBOOK.replace("finance", "ﬁnance"),
        PLAYBOOK.replace("damaged", "dam​aged").replace("queue", "que­ue"),
    ],
    ids=["bytes", "whitespace", "blank-lines", "case", "full-width-and-nbsp", "ligature", "format-chars"],
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


async def test_a_skill_md_copied_under_another_name_carries_the_lineage(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """The frontmatter names the skill, so the whole file never matches; its body does."""
    await _import(settings, store, {}, body=PLAYBOOK)
    saved = await _agent_saves(settings, store, {}, skill_body=PLAYBOOK)
    assert saved["lineage_import"] is True


async def test_a_short_file_copied_verbatim_carries_the_lineage(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Under the floor the bytes still match: a one-line installer is short and someone else's."""
    assert len(normalized_text(SCRIPT)) < COPY_FLOOR_CHARS
    await _import(settings, store, {"scripts/install.sh": SCRIPT, "references/playbook.md": PLAYBOOK})
    saved = await _agent_saves(settings, store, {"scripts/install.sh": SCRIPT})
    assert saved["lineage_import"] is True


async def test_a_short_file_recased_is_not_caught(settings: Settings, store: MemoryObjectStore) -> None:
    await _import(settings, store, {"scripts/install.sh": SCRIPT})
    saved = await _agent_saves(settings, store, {"scripts/install.sh": SCRIPT.upper()})
    assert saved["lineage_import"] is False


async def test_the_floor_is_measured_on_normalized_text(settings: Settings, store: MemoryObjectStore) -> None:
    """`MIT` padded with forty newlines is 44 bytes and 3 normalized characters: its exact bytes
    still match, and a file that only normalizes the same does not."""
    padded = "MIT" + "\n" * 40
    assert len(padded) > COPY_FLOOR_CHARS > len(normalized_text(padded))
    await _import(settings, store, {"references/license.md": padded})
    assert (await _agent_saves(settings, store, {"references/license.md": "mit\n"}))[
        "lineage_import"
    ] is False
    assert (await _agent_saves(settings, store, {"references/license.md": padded}))["lineage_import"] is True


@pytest.mark.parametrize("empty", ["", "\n", "  \n\t\n", "​\n"], ids=["empty", "newline", "blank", "zwsp"])
async def test_an_empty_or_whitespace_file_never_taints(
    empty: str, settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store, {"references/empty.md": empty, "references/playbook.md": PLAYBOOK})
    saved = await _agent_saves(settings, store, {"references/empty.md": empty})
    assert saved["lineage_import"] is False


async def test_at_the_floor_a_normalized_copy_matches(settings: Settings, store: MemoryObjectStore) -> None:
    exact = "x" * COPY_FLOOR_CHARS
    await _import(settings, store, {"references/edge.md": exact})
    assert (await _agent_saves(settings, store, {"references/edge.md": exact.upper()}))[
        "lineage_import"
    ] is True


@pytest.mark.parametrize("asset", [PNG, TINY_PNG], ids=["asset", "short-asset"])
async def test_a_binary_asset_is_matched_by_its_bytes_only(
    asset: str, settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store, {"assets/logo.png": asset})
    rows = await get_skill_library_store(settings, owner=ORG_OWNER).list_files("acme", "refunds", "0.1.0")
    by_path = {r["path"]: r for r in rows}
    assert by_path["assets/logo.png"]["normalized_sha256"] is None
    assert by_path["SKILL.md"]["normalized_sha256"] is not None
    assert (await _agent_saves(settings, store, {"assets/logo.png": asset}))["lineage_import"] is True


async def test_a_file_saved_before_the_normalized_digest_is_matched_by_its_bytes(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """A row from before migration 0032 has no normalized digest, and keeps none: versions are
    immutable. Its bytes still match; a re-cased copy of it is not caught."""
    lib = get_skill_library_store(settings, owner=ORG_OWNER)
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
    assert respaced["lineage_import"] is False, "the documented gap for rows written before 0032"
    copied = await _agent_saves(settings, store, {"references/playbook.md": PLAYBOOK})
    assert copied["lineage_import"] is True
