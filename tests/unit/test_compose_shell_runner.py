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


def test_the_stores_publish_no_host_port() -> None:
    """On Docker Desktop `host.docker.internal` is the host's loopback, where the base file
    publishes Valkey (no password) and Postgres; `shell` must not reach them that way."""
    services = _services()
    for name in ("postgres", "valkey", "minio"):
        assert services[name]["ports"] == [], name
    # The overlay must use `!reset`: a plain `ports: []` is *merged* with the base list.
    text = SELF.read_text(encoding="utf-8")
    for name in ("postgres", "valkey", "minio"):
        assert f"  {name}:\n    ports: !reset []" in text, name


def test_the_shell_service_has_a_process_ceiling() -> None:
    shell = _services()["shell"]
    assert shell["pids_limit"] == 512


# Every key the `shell` service may carry. An allowlist, not a list of dangerous keys: Compose
# grows new ways to share a namespace with another container, and one of them (`pid:
# "service:api"`, and `/proc/<api pid>/environ` is readable) undoes the whole overlay. A key
# not named here fails until someone has decided it is safe and added it.
SHELL_KEYS_ALLOWED = {
    "build",
    "image",
    "command",
    "environment",
    "volumes",
    "networks",
    "cap_drop",
    "security_opt",
    "pids_limit",
    "mem_limit",
    "cpus",
    "healthcheck",
    "restart",
}

# Named as well as excluded by the allowlist, so the reason each one matters is on the record.
SHELL_KEYS_FORBIDDEN = {
    "pid",  # service:api / host -> another process's /proc/<pid>/environ
    "ipc",
    "volumes_from",  # mounts whatever api mounts
    "secrets",
    "privileged",
    "network_mode",  # service:api shares api's loopback and its view of `default`
    "extra_hosts",
    "devices",
    "cap_add",
    "env_file",
}


def test_the_shell_service_carries_only_allowlisted_keys() -> None:
    shell = _services()["shell"]
    assert set(shell) <= SHELL_KEYS_ALLOWED, sorted(set(shell) - SHELL_KEYS_ALLOWED)
    assert not SHELL_KEYS_FORBIDDEN & SHELL_KEYS_ALLOWED
    for key in SHELL_KEYS_FORBIDDEN:
        assert key not in shell, key
    assert shell["cap_drop"] == ["ALL"]
    assert shell["security_opt"] == ["no-new-privileges:true"]
