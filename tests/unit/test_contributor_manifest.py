"""The `contributor` manifest — Felix editing its own checkout.

The agent this manifest compiles can write to the repository it runs on and push to GitHub,
so the assertions here are about the controls, not the plumbing. Every outbound binder in
`builder.py` degrades to a warning rather than an error, which means a misconfigured control
does not fail the build — it produces a quieter agent that still has the dangerous tools.

What a unit test here can and cannot prove, so nobody reads more into a green run than it earns:

* `McpServerRef.tools` is an allowlist over the remote catalogue, so the bound set is the
  manifest's own list and `test_every_bound_write_tool_is_gated` is a real proof: a write tool
  added to the allowlist without an approval rule goes red. What it still cannot see is a tool
  GitHub *renames* — that binds nothing and is logged by `tools_from_mcp_servers`, not failed.
* `READ_ONLY_GITHUB_TOOLS` is the one snapshot left: it says which allowlisted names are reads.
  A read tool GitHub turns into a write would be gated only once someone moves it. Keep the list
  short and keep it read-only by construction (nothing in it takes a body to create or change).
* Approval rules match by glob (`fnmatch`, case-sensitive). The lists stay enumerations anyway:
  `github__*` would gate the read-only tools too, and a pattern here would hide the drift the
  allowlist exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from felix.manifests.loader import load_bundled
from felix.manifests.schema import Manifest, McpServerRef
from felix.manifests.tool_match import select
from felix.secrets import secret_ref_name

ROOT = Path(__file__).resolve().parents[2]

# Allowlisted remote tools that only read. Everything else the manifest allowlists is a write
# and must be gated. Nothing here takes a body that creates or changes a GitHub object.
READ_ONLY_GITHUB_TOOLS = frozenset(
    {
        "get_me",
        "get_file_contents",
        "search_code",
        "search_issues",
        "search_pull_requests",
        "list_issues",
        "issue_read",
        "list_pull_requests",
        "pull_request_read",
        "list_commits",
        "get_commit",
        "list_branches",
        "actions_list",
        "actions_get",
        "get_job_logs",
    }
)

# Remote tools that must never be bound at all: merging and reviewing are a person's act, and
# the rest reach beyond this repository or delete from it. A tool that is not bound needs no
# approval rule, which is a smaller thing to keep true than a rule per tool.
NEVER_BOUND_GITHUB_TOOLS = frozenset(
    {
        "merge_pull_request",
        "pull_request_review_write",
        "add_comment_to_pending_review",
        "add_reply_to_pull_request_comment",
        "request_copilot_review",
        "delete_file",
        "create_repository",
        "fork_repository",
        "run_secret_scanning",
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


def _compose_allowlist() -> list[str]:
    """The prefixes deploy/docker/compose.self.yml exports as FELIX_SHELL_ALLOWED_COMMANDS."""
    text = (ROOT / "deploy/docker/compose.self.yml").read_text(encoding="utf-8")
    m = re.search(r"FELIX_SHELL_ALLOWED_COMMANDS: \$\{FELIX_SHELL_ALLOWED_COMMANDS:-([^}]+)\}", text)
    assert m, "compose.self.yml no longer exports the allowlist this manifest is written against"
    return [p.strip() for p in m.group(1).split(",") if p.strip()]


def test_the_shell_tool_binds_exactly_what_the_builder_allows(manifest: Manifest) -> None:
    """Manifest narrows, operator bounds. Written to be equal, so a prefix added to one side
    and not the other goes red instead of binding a tool that fails on every call."""
    (ref,) = manifest.spec.shell_tools
    assert ref.name == "run"
    assert sorted(ref.commands) == sorted(_compose_allowlist())


def test_no_shell_prefix_can_publish_or_run_arbitrary_code(manifest: Manifest) -> None:
    (ref,) = manifest.spec.shell_tools
    tokens = {tuple(c.split()) for c in ref.commands}
    forbidden = {
        ("git", "push"),
        ("git", "remote"),
        ("git", "config"),
        ("gh",),
        ("python",),
        ("python3",),
        ("uv", "run", "python"),
        ("sh",),
        ("bash",),
    }
    assert not (tokens & forbidden), tokens & forbidden
    assert ("git",) not in tokens and ("uv",) not in tokens and ("uv", "run") not in tokens, (
        "a bare verb covers everything under it"
    )


def test_workspace_writes_are_not_gated_the_host_is(manifest: Manifest) -> None:
    """Deliberate: approvals sit at publication. A person approving every write_file stops
    reading them; the push_files approval carries the diff. deploy/GOVERNANCE.md 'Shell tools'
    is what makes the checkout safe to write to ungated."""
    for tool in ("write_file", "edit_file"):
        assert tool in manifest.spec.tools
        assert tool not in _approval_gated_tools(manifest)
    assert "run" not in _approval_gated_tools(manifest)


def test_anonymous_callers_are_refused(manifest: Manifest) -> None:
    """validate_for_write refuses a shell tool behind allow_anonymous outside development."""
    assert manifest.spec.auth.inbound.allow_anonymous is False


def test_every_bound_write_tool_is_gated(manifest: Manifest) -> None:
    """Allowlist minus the read set is a subset of the approval rule.

    The allowlist is what makes this provable: the bound set is this file's list, not whatever
    the server happens to serve. An empty allowlist or a glob in it would bind tools this test
    cannot name, so both are refused here rather than tolerated.
    """
    github = _github_ref(manifest)
    assert github.tools, "an empty allowlist binds every remote tool, including ones added later"
    globs = [p for p in github.tools if "*" in p or "?" in p]
    assert not globs, f"a pattern binds tools this file cannot name: {globs}"
    writes = {f"github__{n}" for n in github.tools if n not in READ_ONLY_GITHUB_TOOLS}
    assert writes, "the publish path is gone — no write tool is bound"
    missing = writes - _approval_gated_tools(manifest)
    assert not missing, f"ungated GitHub write tools: {sorted(missing)}"


def test_the_tools_a_person_owns_are_not_bound(manifest: Manifest) -> None:
    # `select`, not set intersection: `merge_*` in the allowlist would bind merge_pull_request
    # and pass a literal comparison. Sound on its own, not only because a sibling refuses globs.
    leaked = select(_github_ref(manifest).tools, NEVER_BOUND_GITHUB_TOOLS)
    assert not leaked, f"bound a tool this manifest must never hold: {sorted(leaked)}"


_WRITE_SHAPED = re.compile(r"^(create|update|push|delete|merge|add|run|fork|request)_|_write$")


def test_the_read_only_snapshot_is_read_only_by_construction() -> None:
    """`READ_ONLY_GITHUB_TOOLS` is the one list nothing else checks. A write tool misfiled into
    it would be exempt from the gating proof with every test green, so its members are held to
    the naming GitHub's catalogue uses for writes."""
    misfiled = sorted(n for n in READ_ONLY_GITHUB_TOOLS if _WRITE_SHAPED.search(n))
    assert not misfiled, f"write-shaped names in the read-only set: {misfiled}"


def test_approval_rules_name_only_bound_tools(manifest: Manifest) -> None:
    """A rule for a tool that is not bound is inert and looks like coverage."""
    bound = {f"github__{n}" for n in _github_ref(manifest).tools}
    for rule in manifest.spec.approvals:
        stray = {t for t in rule.tools if t.startswith("github__")} - bound
        assert not stray, f"{rule.id} gates tools that are not bound: {sorted(stray)}"


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
    assert secret_ref_name(_github_ref(manifest).auth) == "GITHUB_MCP_TOKEN"


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
    # `recursion_limit`, not `max_turns`: the latter is read by the multi-agent patterns only.
    assert manifest.spec.recursion_limit and manifest.spec.recursion_limit > 0


def test_code_execution_is_the_shell_tool_and_nothing_else(manifest: Manifest) -> None:
    """Close the other routes to execution, not just the obvious one.

    A client tool is arbitrary command execution on the operator's machine. A stdio MCP server
    is a subprocess spawned as the API process, gated only by operator config rather than by
    this file. A container runs an arbitrary image behind a gateway. `sub_agents` is the
    quietest of all: `builder.py` skips this manifest's own tool resolution when it is set, and
    each child compiles under *its own* approvals block. The one-shot sandbox is gone too: the
    shell tool made it redundant, and two execution routes are two allowlists to keep true.
    """
    spec = manifest.spec
    assert len(spec.shell_tools) == 1
    assert spec.sandboxes == []
    assert spec.client_tools == []
    assert spec.containers == []
    assert spec.queues == []
    assert spec.browser_tools == []
    assert spec.peers == []
    assert spec.sub_agents == []
    assert all(ref.transport in {"http", "sse"} for ref in spec.mcp), "stdio MCP spawns a subprocess"


def _github_ref(manifest: Manifest) -> McpServerRef:
    servers = {ref.name: ref for ref in manifest.spec.mcp}
    assert "github" in servers, "the PR path is the github MCP server"
    return servers["github"]


def _approval_gated_tools(manifest: Manifest) -> set[str]:
    gated: set[str] = set()
    for rule in manifest.spec.approvals:
        gated.update(rule.tools)
    return gated


def test_it_declares_a_token_budget_a_whole_ticket_fits_in(manifest: Manifest) -> None:
    """Unset means 1M, and this agent spends roughly 38k a turn — a run then stops mid-edit at
    about turn 26 with `policy_deny control=limits`, which is how ticket #306's first attempt
    ended. Dropping the declaration would re-impose that silently."""
    from felix.manifests.schema import ABSOLUTE_LIMITS, DEFAULT_LIMITS

    declared = manifest.spec.limits.max_input_tokens
    assert declared is not None, "an unset budget is the 1M default, which is under a run"
    assert DEFAULT_LIMITS["max_input_tokens"] < declared <= ABSOLUTE_LIMITS["max_input_tokens"]
