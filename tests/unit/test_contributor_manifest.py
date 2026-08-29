"""The `contributor` manifest — Felix editing its own checkout.

The agent this manifest compiles can write to the repository it runs on and push to GitHub,
so the assertions here are about the controls, not the plumbing. Every outbound binder in
`builder.py` degrades to a warning rather than an error, which means a misconfigured control
does not fail the build — it produces a quieter agent that still has the dangerous tools.
"""

from __future__ import annotations

import pytest
from felix.manifests.loader import load_bundled
from felix.manifests.schema import Manifest
from felix.secrets import secret_ref_name
from felix.tools.sandboxes import allowed_sandbox_images, assert_sandbox_image_allowed

# Tools that create or move code in the repository. Approval must cover all of them.
MUTATING_GITHUB_TOOLS = frozenset(
    {
        "github__create_branch",
        "github__create_or_update_file",
        "github__push_files",
        "github__create_pull_request",
    }
)


@pytest.fixture
def manifest() -> Manifest:
    return load_bundled("contributor")


def test_loads_under_its_own_name(manifest: Manifest) -> None:
    # `load_bundled` resolves by filename stem, so a metadata name that drifts from the
    # filename produces a manifest nothing can invoke.
    assert manifest.metadata.name == "contributor"
    assert manifest.spec.pattern == "react"


def test_sandbox_image_is_allowed_by_default(manifest: Manifest) -> None:
    """The sandbox must bind with no operator configuration.

    `tools_from_sandboxes` raises on a non-allowlisted image, and `build_agent` catches that
    and logs a warning — so an image outside the default allowlist would silently produce an
    agent with no sandbox at all.
    """
    images = [ref.binding for ref in manifest.spec.sandboxes]
    assert images, "the coding agent must declare a sandbox"
    for image in images:
        assert_sandbox_image_allowed(image, _NoExtraImages())
        assert image in allowed_sandbox_images(_NoExtraImages())


def test_sandbox_declares_no_args_schema(manifest: Manifest) -> None:
    # `args_schema` is accepted by the schema but never read — `tools_from_sandboxes`
    # hardcodes SandboxArgs. Declaring one reads as configuration that does nothing.
    for ref in manifest.spec.sandboxes:
        assert ref.args_schema is None


def test_workspace_writes_require_approval(manifest: Manifest) -> None:
    assert "write_file" in manifest.spec.tools
    assert "write_file" in _approval_gated_tools(manifest)


def test_every_mutating_github_tool_requires_approval(manifest: Manifest) -> None:
    gated = _approval_gated_tools(manifest)
    missing = MUTATING_GITHUB_TOOLS - gated
    assert not missing, f"ungated GitHub write tools: {sorted(missing)}"


def test_approvals_never_run_unattended(manifest: Manifest) -> None:
    # A background run that auto-approves its own commits defeats the gate entirely.
    assert manifest.spec.approvals
    for rule in manifest.spec.approvals:
        assert rule.allow_unattended is False, f"{rule.id} allows unattended approval"


def test_github_token_is_a_secret_ref(manifest: Manifest) -> None:
    servers = {ref.name: ref for ref in manifest.spec.mcp}
    assert "github" in servers, "the PR path is the github MCP server"
    assert secret_ref_name(servers["github"].auth) == "GITHUB_MCP_TOKEN"


def test_untrusted_mcp_output_is_screened(manifest: Manifest) -> None:
    # MCP tools carry transport="mcp", which is outside _TRUSTED_TRANSPORTS. Issue bodies and
    # file contents fetched from GitHub are attacker-controlled text reaching an agent that
    # can write code, so content screening is the control that matters most here.
    assert manifest.spec.content_screening.enabled is True
    assert manifest.spec.content_screening.on_flag in {"quarantine", "block"}


def test_command_screening_keeps_the_defaults(manifest: Manifest) -> None:
    screening = manifest.spec.command_screening
    assert screening.enabled is True
    assert screening.include_defaults is True, "dropping defaults loses destructive-rm and friends"


def test_no_shell_tool_is_bound(manifest: Manifest) -> None:
    """The sandbox is the only code execution this agent gets.

    A client tool is arbitrary command execution on the operator's machine, not a sandbox.
    Adding one here would quietly invalidate what this manifest claims to demonstrate.
    """
    assert manifest.spec.client_tools == []


def _approval_gated_tools(manifest: Manifest) -> set[str]:
    gated: set[str] = set()
    for rule in manifest.spec.approvals:
        gated.update(rule.tools)
    return gated


class _NoExtraImages:
    """Settings stand-in with an empty FELIX_SANDBOX_ALLOWED_IMAGES."""

    sandbox_allowed_images = ""
