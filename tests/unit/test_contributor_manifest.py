"""The `contributor` manifest — Felix editing its own checkout.

The agent this manifest compiles can write to the repository it runs on and push to GitHub,
so the assertions here are about the controls, not the plumbing. Every outbound binder in
`builder.py` degrades to a warning rather than an error, which means a misconfigured control
does not fail the build — it produces a quieter agent that still has the dangerous tools.

Two limits on what any unit test here can prove, worth stating so nobody reads more into a
green run than it earns:

* `MUTATING_GITHUB_TOOLS` is a snapshot of the remote catalog, so the approval test compares
  this file to the manifest and not to what GitHub actually serves. A write tool that GitHub
  adds or renames binds ungated and this suite stays green. The structural fix is a tool
  allowlist on `McpServerRef`, which does not exist yet.
* Approval rules, policies, and screening all match tool names exactly — there are no globs
  in the governance stack — so every list below is an enumeration, not a pattern.
"""

from __future__ import annotations

import pytest
from felix.manifests.loader import load_bundled
from felix.manifests.schema import Manifest
from felix.secrets import secret_ref_name
from felix.tools.sandboxes import DEFAULT_SANDBOX_IMAGE, assert_sandbox_image_allowed

# Every mutating tool the GitHub MCP server exposed when this manifest was written.
MUTATING_GITHUB_TOOLS = frozenset(
    {
        "github__create_branch",
        "github__create_or_update_file",
        "github__push_files",
        "github__delete_file",
        "github__create_repository",
        "github__fork_repository",
        "github__create_pull_request",
        "github__update_pull_request",
        "github__update_pull_request_branch",
        "github__merge_pull_request",
        "github__pull_request_review_write",
        "github__add_comment_to_pending_review",
        "github__add_reply_to_pull_request_comment",
        "github__request_copilot_review",
        "github__issue_write",
        "github__sub_issue_write",
        "github__add_issue_comment",
        "github__run_secret_scanning",
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

    `tools_from_sandboxes` raises on a non-allowlisted image and `build_agent` catches that
    and logs a warning, so an image outside the default allowlist silently produces an agent
    with no sandbox at all.
    """
    images = [ref.binding for ref in manifest.spec.sandboxes]
    assert images, "the coding agent must declare a sandbox"
    for image in images:
        assert image == DEFAULT_SANDBOX_IMAGE
        assert_sandbox_image_allowed(image, _NoExtraImages())


def test_workspace_writes_require_approval(manifest: Manifest) -> None:
    assert "write_file" in manifest.spec.tools
    assert "write_file" in _approval_gated_tools(manifest)


def test_known_mutating_github_tools_are_gated(manifest: Manifest) -> None:
    """Every write tool in the recorded catalog is named in an approval rule.

    This cannot prove totality — see the module docstring. It proves the manifest has not
    dropped one of the tools we know exist.
    """
    missing = MUTATING_GITHUB_TOOLS - _approval_gated_tools(manifest)
    assert not missing, f"ungated GitHub write tools: {sorted(missing)}"


def test_approval_rules_cannot_be_silently_disarmed(manifest: Manifest) -> None:
    """`when_args` and a missing TTL are the two ways a rule stops firing without moving.

    `builder.py` returns the *unwrapped* executor when a rule's `when_args` are not all
    present in the call, so one plausible-looking arg name turns a gate into a no-op that
    still passes `validate-manifest`. `ttl_seconds` is what makes an unanswered approval fail
    closed rather than hang.
    """
    assert manifest.spec.approvals
    for rule in manifest.spec.approvals:
        assert rule.when_args == [], f"{rule.id} only fires when {rule.when_args} are present"
        assert rule.ttl_seconds and rule.ttl_seconds > 0, f"{rule.id} has no TTL"


def test_unattended_approval_is_actually_enforced(manifest: Manifest) -> None:
    """`allow_unattended` is inert unless eu_ai_act is on at risk_tier high.

    It is read in exactly one place — the governance validator — and never by
    `apply_approvals`. Without the governance block below it is decoration, and a field that
    looks like an access control and is not one is worse than a missing feature.
    """
    gov = manifest.spec.governance
    assert "eu_ai_act" in gov.frameworks
    assert gov.risk_tier == "high"
    for rule in manifest.spec.approvals:
        assert rule.allow_unattended is False, f"{rule.id} allows unattended approval"


def test_github_token_is_a_secret_ref(manifest: Manifest) -> None:
    servers = {ref.name: ref for ref in manifest.spec.mcp}
    assert "github" in servers, "the PR path is the github MCP server"
    assert secret_ref_name(servers["github"].auth) == "GITHUB_MCP_TOKEN"


def test_untrusted_mcp_output_is_screened(manifest: Manifest) -> None:
    """MCP tools carry transport="mcp", which is outside `_TRUSTED_TRANSPORTS`.

    Issue bodies and file contents fetched from GitHub are attacker-controlled text reaching
    an agent that can write code. A non-empty `tools` list is the trap: it screens only the
    names it lists and skips the untrusted-transport rule entirely, so every `github__` tool
    would go unscreened while `enabled` still reads true.
    """
    screening = manifest.spec.content_screening
    assert screening.enabled is True
    assert screening.tools == [], "an explicit tools list drops the untrusted-transport default"
    assert screening.on_flag == "quarantine", "a flagged fetch should degrade, not kill the run"


def test_command_screening_keeps_the_defaults(manifest: Manifest) -> None:
    screening = manifest.spec.command_screening
    assert screening.enabled is True
    assert screening.include_defaults is True, "dropping defaults loses destructive-rm and friends"
    denied = [rule.pattern for rule in screening.rules if rule.decision == "deny"]
    assert denied, "the manifest's own deny rules were removed"


def test_the_run_is_bounded(manifest: Manifest) -> None:
    # Every Limits field defaults to None, so deleting the block removes the only ceiling on a
    # self-editing loop without changing a single assertion elsewhere.
    limits = manifest.spec.limits
    assert limits.max_tool_calls and limits.max_tool_calls > 0
    assert limits.max_wall_clock_seconds and limits.max_wall_clock_seconds > 0
    assert manifest.spec.max_turns and manifest.spec.max_turns > 0


def test_code_execution_is_the_sandbox_and_nothing_else(manifest: Manifest) -> None:
    """Close the other routes to execution, not just the obvious one.

    A client tool is arbitrary command execution on the operator's machine. A stdio MCP server
    is a subprocess spawned as the API process, gated only by operator config rather than by
    this file. A container runs an arbitrary image behind a gateway. `sub_agents` is the
    quietest of all: `builder.py` skips this manifest's own tool resolution when it is set, and
    each child compiles under *its own* approvals block, so a child holding `write_file` with no
    approvals bypasses every gate above.
    """
    spec = manifest.spec
    assert spec.client_tools == []
    assert spec.containers == []
    assert spec.queues == []
    assert spec.browser_tools == []
    assert spec.peers == []
    assert spec.sub_agents == []
    assert all(ref.transport in {"http", "sse"} for ref in spec.mcp), "stdio MCP spawns a subprocess"


def _approval_gated_tools(manifest: Manifest) -> set[str]:
    gated: set[str] = set()
    for rule in manifest.spec.approvals:
        gated.update(rule.tools)
    return gated


class _NoExtraImages:
    """Settings stand-in with an empty FELIX_SANDBOX_ALLOWED_IMAGES.

    Real `Settings` would read the developer's `.env`, where `FELIX_SANDBOX_ALLOWED_IMAGES` is
    a live key — making this test pass or fail depending on the machine.
    """

    sandbox_allowed_images = ""
