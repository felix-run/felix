"""The builder overlay runs shell tools in a container that holds no secrets.

That sentence is the whole security property, and it is a property of a YAML file: one more
line under `shell.environment`, one more volume, or the `default` network, and the container
the agent's code runs in can read a token or reach Valkey again — with every Python test green.
So it is pinned here, structurally, as the other compose tests pin theirs. CI's `compose config`
proves the file parses; this proves what it says.
"""

from __future__ import annotations

from pathlib import Path

from tests.compose_yaml import load_compose

SELF = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "compose.self.yml"

# What the runner, and the entrypoint that prepares the workspace before it, read. Nothing here
# is a credential except the runner token, which authenticates callers *to* the runner and is
# readable by the code the runner execs anyway (deploy/GOVERNANCE.md "Shell tools").
SHELL_ENV_ALLOWED = {
    "FELIX_SHELL_ALLOWED_COMMANDS",
    "FELIX_SHELL_RUNNER_TOKEN",
    "FELIX_WORKSPACE_ROOT",
    "FELIX_SELF_REPO",
    "FELIX_SELF_BRANCH",
    "FELIX_SELF_GIT_NAME",
    "FELIX_SELF_GIT_EMAIL",
    "FELIX_SELF_SYNC",
}


def _services() -> dict:
    return load_compose(SELF)["services"]


def test_the_shell_service_environment_is_an_allowlist() -> None:
    env = _services()["shell"]["environment"]
    assert set(env) <= SHELL_ENV_ALLOWED, sorted(set(env) - SHELL_ENV_ALLOWED)
    for secret in ("GITHUB_MCP_TOKEN", "FELIX_DATABASE_URL", "FELIX_REDIS_URL", "FELIX_AUTH_API_KEYS"):
        assert secret not in env


def test_the_shell_service_mounts_only_the_workspace_and_no_shared_network() -> None:
    shell = _services()["shell"]
    assert shell["volumes"] == ["felix-self-workspace:/workspace"]
    # Not `default`: Postgres, Valkey (no password) and MinIO live there.
    assert shell["networks"] == ["shell"]
    assert shell["command"] == ["felix-shell-runner"]
    assert "env_file" not in shell
    assert "http://127.0.0.1:8080/health" in " ".join(shell["healthcheck"]["test"])


def test_api_and_worker_send_shell_tools_to_it_and_wait_for_it() -> None:
    services = _services()
    for name in ("api", "worker"):
        svc = services[name]
        env = svc["environment"]
        assert env["FELIX_SHELL_RUNNER_URL"] == "http://shell:8080", name
        assert env["FELIX_SHELL_RUNNER_TOKEN"] == services["shell"]["environment"]["FELIX_SHELL_RUNNER_TOKEN"]
        # The same allowlist on both sides, so the runner's re-check refuses nothing the API allowed.
        assert (
            env["FELIX_SHELL_ALLOWED_COMMANDS"]
            == services["shell"]["environment"]["FELIX_SHELL_ALLOWED_COMMANDS"]
        )
        # api/worker run no git against the agent-writable checkout at startup.
        assert env["FELIX_SELF_PREPARE_WORKSPACE"] == "0", name
        assert svc["depends_on"]["shell"]["condition"] == "service_healthy", name
        assert "shell" in svc["networks"] and "default" in svc["networks"], name


def test_the_runner_token_is_required_not_defaulted() -> None:
    """`:?` makes Compose refuse to start; `:-` would start an endpoint with a known bearer."""
    token = _services()["shell"]["environment"]["FELIX_SHELL_RUNNER_TOKEN"]
    assert token.startswith("${FELIX_SHELL_RUNNER_TOKEN:?"), token
