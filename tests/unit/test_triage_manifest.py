"""The `triage` manifest — Felix proposing work on its own checkout.

It may file issues and comments, every one of them approval-gated, and nothing else: no file
write, no shell, no sandbox, no branch, no pull request. The assertions are about that shape,
because every outbound binder degrades to a warning and a misconfigured control produces a
quieter agent with the same tools.
"""

from __future__ import annotations

import pytest
from felix.manifests.loader import load_bundled
from felix.manifests.schema import Manifest, McpServerRef
from felix.manifests.tool_match import select
from felix.secrets import secret_ref_name

READ_ONLY_GITHUB_TOOLS = frozenset(
    {
        "get_me",
        "get_file_contents",
        "search_code",
        "search_issues",
        "list_issues",
        "issue_read",
        "list_pull_requests",
        "pull_request_read",
        "list_workflow_runs",
        "get_workflow_run",
        "list_workflow_jobs",
        "get_job_logs",
    }
)
# The only writes a proposer holds.
ISSUE_WRITERS = frozenset({"issue_write", "sub_issue_write", "add_issue_comment"})
# Anything that reaches contents, branches, pull requests or reviews stays unbound.
NEVER_BOUND = frozenset(
    {
        "create_branch",
        "push_files",
        "create_or_update_file",
        "delete_file",
        "create_pull_request",
        "update_pull_request",
        "update_pull_request_branch",
        "merge_pull_request",
        "pull_request_review_write",
        "create_repository",
        "fork_repository",
    }
)


@pytest.fixture
def manifest() -> Manifest:
    return load_bundled("triage")


def _github(manifest: Manifest) -> McpServerRef:
    servers = {ref.name: ref for ref in manifest.spec.mcp}
    assert "github" in servers
    return servers["github"]


def _gated(manifest: Manifest) -> set[str]:
    return {t for rule in manifest.spec.approvals for t in rule.tools}


def test_loads_under_its_own_name(manifest: Manifest) -> None:
    assert manifest.metadata.name == "triage"
    assert manifest.spec.pattern == "react"


def test_it_holds_no_way_to_change_a_file_or_run_anything(manifest: Manifest) -> None:
    spec = manifest.spec
    assert "write_file" not in spec.tools
    assert spec.sandboxes == [] and spec.shell_tools == [] and spec.containers == []
    assert spec.client_tools == [] and spec.queues == [] and spec.browser_tools == []
    assert spec.peers == [] and spec.sub_agents == []
    assert all(ref.transport in {"http", "sse"} for ref in spec.mcp), "stdio spawns a subprocess"


def test_the_allowlist_is_reads_plus_the_three_issue_writers(manifest: Manifest) -> None:
    tools = _github(manifest).tools
    assert tools, "an empty allowlist binds every remote tool"
    assert not [p for p in tools if "*" in p or "?" in p], "a pattern binds tools this file cannot name"
    assert set(tools) - READ_ONLY_GITHUB_TOOLS == ISSUE_WRITERS
    assert select(tools, NEVER_BOUND) == set()


def test_every_writer_is_gated_and_nothing_else_is_named(manifest: Manifest) -> None:
    gated = _gated(manifest)
    assert gated == {f"github__{n}" for n in ISSUE_WRITERS}


def test_approvals_cannot_be_silently_disarmed(manifest: Manifest) -> None:
    assert manifest.spec.approvals
    for rule in manifest.spec.approvals:
        assert rule.when_args == [], f"{rule.id} only fires when {rule.when_args} are present"
        assert rule.ttl_seconds and rule.ttl_seconds > 0
        assert rule.allow_unattended is False
    gov = manifest.spec.governance
    assert "eu_ai_act" in gov.frameworks and gov.risk_tier == "high", "allow_unattended is inert otherwise"


def test_untrusted_github_output_is_screened(manifest: Manifest) -> None:
    screening = manifest.spec.content_screening
    assert screening.enabled is True
    assert screening.tools == [], "an explicit tools list drops the untrusted-transport default"
    assert screening.on_flag == "quarantine"


def test_the_run_is_bounded(manifest: Manifest) -> None:
    limits = manifest.spec.limits
    assert limits.max_tool_calls and limits.max_tool_calls > 0
    assert limits.max_wall_clock_seconds and limits.max_wall_clock_seconds > 0
    assert manifest.spec.max_turns and manifest.spec.max_turns > 0


def test_the_token_is_a_secret_ref_and_the_skill_is_declared(manifest: Manifest) -> None:
    assert secret_ref_name(_github(manifest).auth) == "GITHUB_MCP_TOKEN"
    assert "felix-self" in {s.name for s in manifest.spec.skills}
