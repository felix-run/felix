"""The example agents: `researcher`, `automator` and `summarizer` in `manifests/`, and the
connectors plugin's `concierge`, `travel`, `markets` and `inbox` (`register_manifest_dir`).

Each is compiled the way a request compiles it -- the bundled loader, the default tool provider,
`build_agent` -- and held to what its comment promises: every tool that changes something is
behind an approval (or is on the short list below, with its reason), what it reads from someone
else is screened, and nobody anonymous can reach it.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from felix.manifests.loader import clear_bundled_cache, load_bundled

from tests.support.factories import make_settings
from tests.support.optional_deps import require_optional

CORE = ("researcher", "automator", "summarizer")
#: The connectors plugin's agents, and the extras each one's tools need beyond the plugin.
CONNECTORS = {
    "concierge": ("google-connectors", "icloud-connectors", "ics-connectors"),
    "travel": (),
    "markets": (),
    "inbox": ("google-connectors",),
}
_EXTRA_MODULE = {
    "google-connectors": "googleapiclient",
    "icloud-connectors": "icalendar",
    "ics-connectors": "icalendar",
}

#: (agent, tool) pairs that change something and run with no approval, on purpose. Keyed by the
#: agent: each reason holds for that manifest, not for every one that binds the tool.
UNGATED = {
    # Saving a fact about the person to their own memory, as `assistant` does; `forget` is gated.
    ("concierge", "remember"): "the person's own memory",
    ("markets", "remember"): "the person's own watchlist",
    # Stops a task the agent set up: it can only reduce what runs as the person.
    ("concierge", "cancel_scheduled"): "only removes",
    ("markets", "cancel_scheduled"): "only removes",
    # A scheduled briefing or alert fires with nobody watching to approve it, and a notice goes
    # only to the person the run acts for (`notify` caps it per run and per hour).
    ("concierge", "notify"): "only to the person",
    ("markets", "notify"): "only to the person",
    # A GET held to `network.allow_hosts` (asserted below); not read-only, since a fetch is not
    # replay-safe.
    ("researcher", "web_fetch"): "confined to allow_hosts",
}


#: Agents whose every tool runs in-process (governance's trusted transports).
TRUSTED_ONLY = {
    "summarizer": "the child has no tools; it reads only what its parent hands it",
    # The workflow tools are in-process; a run's steps compile their own agents, each with its
    # own screening, and the answer comes back as this agent's own tool result.
    "automator": "workflow tools are in-process",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    from felix.config import get_settings

    # The plugin builds its tools from the process settings, as production does.
    monkeypatch.setenv("FELIX_MARKET_API_KEY", "test-key")
    get_settings.cache_clear()
    clear_bundled_cache()
    yield
    get_settings.cache_clear()
    clear_bundled_cache()


async def _compile(name: str) -> tuple[Any, Any]:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.builtins import default_tool_provider

    m = load_bundled(name)
    agent = await build_agent(
        m, deps=BuildDeps(tools=default_tool_provider(), settings=make_settings(), tenant_id="acme")
    )
    return m, agent


def _rules(m: Any) -> list[Any]:
    """Every approval rule the compile applies: the manifest's, and those its blocks add."""
    from felix.governance.permission_mode import builtin_approval_rules

    rules = [*m.spec.approvals, *builtin_approval_rules(m.spec, list(m.spec.approvals))]
    if m.spec.workflow_authoring is not None:
        from felix.workflows.agent_tools import workflow_run_rule

        run = workflow_run_rule(m.spec.workflow_authoring, list(m.spec.approvals))
        rules += [run] if run is not None else []
    return rules


@pytest.fixture(params=[*CORE, *CONNECTORS])
def name(request: Any) -> str:
    if request.param in CONNECTORS:
        require_optional("felix_connectors", "connectors")
        for extra in CONNECTORS[request.param]:
            require_optional(_EXTRA_MODULE[extra], extra)
    return request.param


async def test_it_compiles_and_binds_every_tool_it_names(name: str) -> None:
    m, agent = await _compile(name)
    bound = {t.name for t in agent.tools}
    assert set(m.spec.tools) <= bound, set(m.spec.tools) - bound


async def test_everything_that_changes_something_waits_for_the_person(name: str) -> None:
    from felix.manifests.tool_match import matches_any

    m, agent = await _compile(name)
    rules = _rules(m)
    exempt = {tool for agent_name, tool in UNGATED if agent_name == name}
    writers = {t.name: t for t in agent.tools if not t.read_only}
    ungated = {n for n in writers if not any(matches_any(r.tools, n) for r in rules)}
    assert ungated <= exempt, ungated - exempt
    if "web_fetch" in exempt:
        assert m.spec.network.allow_hosts, "web_fetch is exempt only where allow_hosts confines it"
    # A writer acting as the person at another service is refused outright in a run nobody
    # watches, and each approval covers one call by the person who gave it.
    held = [r for r in rules if r.tools and not r.allow_unattended and r.one_shot and r.bind_principal]
    for tool in (t for t in writers.values() if (t.source or "").startswith("connectors:")):
        assert any(matches_any(r.tools, tool.name) for r in held), tool.name


async def test_whatever_reads_someone_elses_words_is_screened(name: str) -> None:
    from felix.manifests.builder import _is_untrusted_tool

    m, agent = await _compile(name)
    untrusted = [t.name for t in agent.tools if _is_untrusted_tool(t)]
    if name in TRUSTED_ONLY:
        assert untrusted == [], TRUSTED_ONLY[name]
        return
    assert untrusted
    assert m.spec.content_screening.enabled


async def test_the_mail_agents_score_what_anyone_can_mail_them() -> None:
    require_optional("felix_connectors", "connectors")
    for agent_name in ("concierge", "inbox"):
        screening = load_bundled(agent_name).spec.content_screening
        assert screening.model, agent_name
        assert {"connectors__gmail_read", "connectors__gmail_search"} <= set(screening.model_tools)


def test_nobody_anonymous_reaches_it(name: str) -> None:
    m = load_bundled(name)
    assert m.spec.auth.inbound.allow_anonymous is False
    assert m.spec.limits.max_cost_usd is not None and m.spec.limits.max_cost_usd > 0


async def test_researchers_fetch_is_held_to_its_list_and_its_child_to_the_same() -> None:
    from felix.security.host_allowlist import EgressBound, HostAllowlist

    m, agent = await _compile("researcher")
    hosts = list(m.spec.network.allow_hosts or [])
    assert "*.wikipedia.org" in hosts
    bound = EgressBound((HostAllowlist(tuple(hosts)),))
    assert bound.allows("en.wikipedia.org") and not bound.allows("example.com")
    assert m.spec.delegation is not None
    assert [d.name for d in m.spec.delegation.agents] == ["summarizer"]
    assert "task" in {t.name for t in agent.tools}
    assert load_bundled("summarizer").spec.auth.inbound.allow_anonymous is False


async def test_a_fetch_confined_by_allow_hosts_is_not_logged_as_reaching_anywhere_and_wildcards_are(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from felix.manifests.schema import HttpFetchToolRef
    from felix.security.host_allowlist import EgressBound, HostAllowlist
    from felix.tools.http_fetch import tools_from_http_fetch_refs

    caplog.set_level(logging.INFO, logger="felix.tools.http_fetch")
    ref = HttpFetchToolRef(name="web_fetch", allow_any_host=True)
    tools_from_http_fetch_refs([ref], egress=EgressBound((HostAllowlist(("docs.python.org",)),)))
    assert [r.levelname for r in caplog.records] == ["INFO"]
    assert "takes any path on the hosts network.allow_hosts lists" in caplog.text
    caplog.clear()
    # researcher's list holds `*.wikipedia.org`: a wildcard is still a review question.
    await _compile("researcher")
    (warned,) = [r for r in caplog.records if r.levelname == "WARNING" and "allow_any_host" in r.getMessage()]
    assert "*.wikipedia.org" in warned.getMessage()
    assert "may fetch any address" not in caplog.text


async def test_markets_answers_pass_the_advice_judge_before_they_reach_the_person() -> None:
    require_optional("felix_connectors", "connectors")
    m, _agent = await _compile("markets")
    judges = {j.name: j for j in m.spec.guardrails.judges}
    assert judges["no_personal_investment_advice"].final_response is True


async def test_markets_without_its_key_fails_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    require_optional("felix_connectors", "connectors")
    from felix.config import get_settings
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.builtins import default_tool_provider

    monkeypatch.delenv("FELIX_MARKET_API_KEY")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="FELIX_MARKET_API_KEY"):
        await build_agent(
            load_bundled("markets"),
            deps=BuildDeps(tools=default_tool_provider(), settings=make_settings(), tenant_id="acme"),
        )
