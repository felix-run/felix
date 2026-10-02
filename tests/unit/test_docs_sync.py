"""`felix_client.docs_sync`: what of an MDX page reaches the corpus, and under which URL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix_cli.main import app
from felix_client import docs_sync
from felix_client.docs_sync import SiteUrlError, SyncResult, check_site_url, mdx_to_text, page_url, read_pages
from typer.testing import CliRunner

_PAGE = """---
title: "Auth"
description: Inbound auth modes and scopes.
sidebar:
  order: 7
---
import { Aside } from '@astrojs/starlight/components';
import approvalCard from '../../assets/approval-card.png';

## Modes

<Aside type="note">
Keep the token short-lived.
</Aside>

:::caution[Consent phishing]
Never enter a code you did not request.
:::

```tsx
import { Aside } from '@astrojs/starlight/components';
<Aside type="tip">
:::note
```
"""


def test_mdx_keeps_the_prose_and_code_and_drops_the_markup() -> None:
    title, text, _ = mdx_to_text(_PAGE)
    assert title == "Auth"
    assert text.startswith("Inbound auth modes and scopes.")
    assert "## Modes" in text and "Keep the token short-lived." in text
    # A directive's label is the reader's heading for it; the fences are markup.
    assert "Consent phishing:" in text and ":::" not in text.split("```")[0]
    assert "Never enter a code you did not request." in text
    # ESM and component-tag lines go; nested frontmatter (sidebar.order) never arrives.
    assert "approval-card.png" not in text
    prose, code = text.split("```tsx")
    assert "<Aside" not in prose and "@astrojs" not in prose and "order" not in prose
    # Inside a code fence every line is kept, even ones the prose filters would drop.
    assert code.splitlines()[1:4] == [
        "import { Aside } from '@astrojs/starlight/components';",
        '<Aside type="tip">',
        ":::note",
    ]


def test_a_page_without_frontmatter_has_no_title() -> None:
    assert mdx_to_text("# Hello\n\nbody") == (None, "# Hello\n\nbody", None)


def test_a_fence_inside_a_longer_fence_does_not_flip_the_state() -> None:
    """A page showing how to write a fence: the inner ``` must not end the outer ~~~ block, or
    every code line after it is read as prose and stripped."""
    raw = '~~~md\n```\n~~~\n\n```bash\nexport FELIX_API_KEY=abc\nimport x from "y"\n```\n'
    _, text, _ = mdx_to_text(raw)
    assert "export FELIX_API_KEY=abc" in text and 'import x from "y"' in text


def test_a_fence_closes_only_on_its_own_character_and_length() -> None:
    # A shorter run of the same character is content, not a close.
    _, text, _ = mdx_to_text("````md\n```\n<Shown />\n````\n<Card />\n")
    assert "<Shown />" in text and "<Card" not in text
    # So is a run of the other character.
    _, text, _ = mdx_to_text("~~~md\n```\n<Shown />\n~~~\n<Card />\n")
    assert "<Shown />" in text and "<Card" not in text


def test_multi_line_imports_and_tags_are_dropped_but_a_label_is_kept() -> None:
    raw = (
        "import {\n  Aside,\n  Steps,\n} from '@astrojs/starlight/components';\n"
        "export const meta = {\n  a: 1,\n};\n"
        '<Card\n  icon="x"\n  title="Install"\n>\nRun the installer.\n</Card>\n'
        "<TabItem label='npm'>\n"
    )
    _, text, _ = mdx_to_text(raw)
    assert text.splitlines() == ["Install:", "Run the installer.", "npm:"]


def test_plain_markdown_keeps_lines_mdx_would_read_as_code() -> None:
    raw = "export FOO=1 in your shell.\nimport x from y is the syntax.\n<Foo>\n:::note\nkept\n:::\n"
    _, text, _ = mdx_to_text(raw, mdx=False)
    assert text.splitlines() == [
        "export FOO=1 in your shell.",
        "import x from y is the syntax.",
        "<Foo>",
        "kept",
    ]


def test_a_block_scalar_title_is_not_a_title() -> None:
    title, _, _ = mdx_to_text("---\ntitle: >-\n  Long\n---\nBody.")
    assert title is None


def test_a_bom_does_not_hide_the_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "x.mdx").write_bytes("\ufeff---\ntitle: T\nslug: custom/path\n---\nBody.".encode())
    (page,), _ = read_pages(tmp_path, site_url="https://d.example")
    assert (page.title, page.text, page.source) == ("T", "Body.", "https://d.example/custom/path/")


@pytest.mark.parametrize(
    ("rel", "url"),
    [
        ("guide/rest-api.mdx", "https://docs.example/guide/rest-api/"),
        ("Guide/Deploy.md", "https://docs.example/guide/deploy/"),
        ("guide/index.mdx", "https://docs.example/guide/"),
        ("index.mdx", "https://docs.example/"),
        ("guide/Foo Bar.md", "https://docs.example/guide/foo-bar/"),
        ("guide/a (old).md", "https://docs.example/guide/a-old/"),
    ],
)
def test_a_page_is_sourced_at_its_public_url(rel: str, url: str) -> None:
    root = Path("/site")
    assert page_url("https://docs.example/", root, root / rel) == url


def test_a_frontmatter_slug_replaces_the_path() -> None:
    root = Path("/site")
    assert page_url("https://d.example", root, root / "x.mdx", slug="/Custom/Path/") == (
        "https://d.example/custom/path/"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "https://",
        "https:",
        "https:/",
        "",
        "docs.example",
        "ftp://docs.example",
        "https://d.example/?a=1",
        "https://d.example/#x",
    ],
)
def test_a_site_url_a_prune_could_not_be_scoped_to_is_refused(bad: str) -> None:
    """`https://$DOCS_HOST` with the variable unset made the prune prefix every https URL."""
    with pytest.raises(SiteUrlError):
        check_site_url(bad)


def test_two_files_on_one_page_are_skipped_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides.md").write_text("---\ntitle: Guides\n---\nOne.")
    (tmp_path / "guides" / "index.md").write_text("---\ntitle: Guides\n---\nTwo.")
    pages, skipped = read_pages(tmp_path, site_url="https://d.example")
    assert [p.text for p in pages] == ["Two."]
    assert [(p.name, why) for p, why in skipped] == [
        ("guides.md", "same page as guides/index.md (https://d.example/guides/)")
    ]


def test_hidden_and_underscore_paths_are_not_pages(tmp_path: Path) -> None:
    for rel in (".git/x.md", "_drafts/y.mdx", "node/.cache/z.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("Body.")
    (tmp_path / "real.md").write_text("Body.")
    pages, skipped = read_pages(tmp_path, site_url="https://d.example")
    assert [p.source for p in pages] == ["https://d.example/real/"]
    assert skipped == []


def test_read_pages_skips_the_not_found_page_and_empty_ones(tmp_path: Path) -> None:
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "a.mdx").write_text("---\ntitle: A\n---\nBody of a.")
    (tmp_path / "guide" / "untitled-page.md").write_text("Body.")
    (tmp_path / "404.mdx").write_text("---\ntitle: Page not found\n---\nNothing here.")
    (tmp_path / "empty.mdx").write_text("---\ntitle: Empty\n---\nimport X from 'x';\n<X />\n")
    (tmp_path / "notes.txt").write_text("not a page")
    pages, skipped = read_pages(tmp_path, site_url="https://docs.example")
    assert [(p.title, p.source) for p in pages] == [
        ("A", "https://docs.example/guide/a/"),
        ("Untitled Page", "https://docs.example/guide/untitled-page/"),
    ]
    assert sorted((p.name, why) for p, why in skipped) == [
        ("404.mdx", "not-found page"),
        ("empty.mdx", "no text after stripping markup"),
    ]


# --- the command --------------------------------------------------------------------------


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def respond(result: SyncResult) -> None:
        async def fake_sync(client: Any, pages: Any, **kwargs: Any) -> SyncResult:
            calls.append({"client": client, "pages": list(pages), **kwargs})
            return result

        monkeypatch.setattr(docs_sync, "sync_pages", fake_sync)

    respond(SyncResult(ingested=[("https://docs.example/a/", 3)]))
    calls.append({"respond": respond})
    return calls


def _site(tmp_path: Path) -> Path:
    (tmp_path / "a.mdx").write_text("---\ntitle: A\n---\nBody.")
    return tmp_path


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["ingest-docs", *args])


def test_the_command_passes_its_flags_and_key_through(synced: list[dict[str, Any]], tmp_path: Path) -> None:
    result = _run(
        str(_site(tmp_path)),
        "--site-url",
        "https://docs.example",
        "--url",
        "https://felix.example",
        "--api-key",
        "k",
        "--prune",
    )
    assert result.exit_code == 0, result.output
    call = synced[-1]
    assert (call["site_url"], call["prune"], call["dry_run"], call["max_prune"]) == (
        "https://docs.example",
        True,
        False,
        10,
    )
    assert (call["client"].base_url, call["client"].api_key) == ("https://felix.example", "k")
    assert [p.title for p in call["pages"]] == ["A"]
    assert "ingested https://docs.example/a/ (3 chunks)" in result.stdout


def test_a_failed_page_exits_1_and_names_it(synced: list[dict[str, Any]], tmp_path: Path) -> None:
    synced[0]["respond"](SyncResult(failed=[("https://docs.example/a/", "HTTP 403: forbidden")]))
    result = _run(str(_site(tmp_path)), "--site-url", "https://docs.example")
    assert result.exit_code == 1
    assert "failed https://docs.example/a/: HTTP 403" in result.stderr


def test_a_directory_with_no_pages_exits_1(synced: list[dict[str, Any]], tmp_path: Path) -> None:
    result = _run(str(tmp_path), "--site-url", "https://docs.example")
    assert result.exit_code == 1
    assert len(synced) == 1  # only the fixture's own entry: nothing was synced


def test_a_degenerate_site_url_is_a_usage_error_before_anything_is_sent(
    synced: list[dict[str, Any]], tmp_path: Path
) -> None:
    result = _run(str(_site(tmp_path)), "--site-url", "https://", "--prune")
    assert result.exit_code == 2
    assert "--site-url must be an http(s) URL with a host" in result.stderr
    assert len(synced) == 1


def test_max_prune_is_passed_through(synced: list[dict[str, Any]], tmp_path: Path) -> None:
    assert _run(str(_site(tmp_path)), "--site-url", "https://d.example", "--max-prune", "50").exit_code == 0
    assert synced[-1]["max_prune"] == 50
