"""A skill on GitHub is browsed, imported, published over HTTP, and used by a later session.

The chain no unit test holds end to end: `GET /skill-library/-/browse` and `POST /-/import` on
the zero-argument app → the scope gate → `skills/importer.py` pinned to one commit → a draft row
and its bytes in the stores the API booted with → the publish gate, stricter for an import → a
fresh compile's catalog listing the imported skill → `activate_skill` returning its body.

GitHub is `tests/skill_import_fake.py`, served to the production path's client factory
(`github.github_client`) and nothing else; every other hop is the real one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.skill_import_fake import FakeRepos, skill_md

NAME = "invoice-triage"
SOURCE = f"github:acme/skills/skills/{NAME}"
BODY = (
    "# Invoice triage\n\nUse this when an invoice arrives.\n\n## Steps\n\n"
    "1. Read the vendor and the amount.\n2. Route amounts over 500 to the finance queue.\n"
)
ENV = {"FELIX_SKILL_IMPORT_SOURCES": "github:acme/*"}


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeRepos:
    fake = FakeRepos()
    fake.push(
        "acme/skills",
        {
            f"skills/{NAME}/SKILL.md": skill_md(NAME, "Route incoming invoices.", BODY),
            f"skills/{NAME}/references/queues.md": b"# Queues\n\nfinance\n",
            f"skills/{NAME}/tests/test_it.py": b"assert True\n",
            "plugins/billing/skills/refunds/SKILL.md": skill_md("refunds", "Issue refunds."),
        },
        license="Apache-2.0",
    )
    fake.serve(monkeypatch)
    return fake


def _manifest() -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-importer"},
            "spec": {
                "pattern": "react",
                "tools": ["list_skills", "activate_skill"],
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )


def _tool_result(prompt: list[Any]) -> str:
    return next(str(m.content) for m in reversed(prompt) if getattr(m, "role", "") == "tool")


async def test_an_imported_skill_is_browsed_published_and_used(boot: Any, gh: FakeRepos) -> None:
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="list_skills", args={})]),
        ScriptedTurn(tool_calls=[ToolCall(id="c2", name="activate_skill", args={"name": NAME})]),
        ScriptedTurn(content="routed"),
    ]
    async with boot(script, env=ENV, manifests={"e2e-importer": _manifest()}) as app:
        browsed = await app.client.get("/skill-library/-/browse", params={"source": "github:acme/skills"})
        assert browsed.status_code == 200, browsed.text
        listing = browsed.json()
        assert listing["license"] == "Apache-2.0"
        assert [(i["name"], i["source"]) for i in listing["items"]] == [
            (NAME, SOURCE),
            ("refunds", "github:acme/skills/plugins/billing/skills/refunds"),
        ]

        imported = await app.client.post("/skill-library/-/import", json={"source": SOURCE, "publish": True})
        assert imported.status_code == 201, imported.text
        row = imported.json()
        assert (row["version"], row["source"], row["status"], row["published"]) == (
            "0.1.0",
            "import",
            "published",
            True,
        )
        assert row["origin_commit"] == listing["commit"] and row["origin_license"] == "Apache-2.0"
        assert row["dropped_files"] == ["tests/test_it.py"]
        assert [f["path"] for f in row["files"]] == ["SKILL.md", "references/queues.md"]

        detail = (await app.client.get(f"/skill-library/{NAME}/versions/0.1.0")).json()
        assert (detail["origin_source"], detail["origin_ref"]) == (SOURCE, "main")
        listed = (await app.client.get("/skill-library", params={"source": "import"})).json()["items"]
        assert [s["name"] for s in listed] == [NAME]

        again = await app.client.post("/skill-library/-/import", json={"source": SOURCE, "publish": True})
        assert again.status_code == 200, again.text
        assert again.json()["unchanged"] is True and again.json()["published"] is False

        chat = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-importer", "messages": [{"role": "user", "content": "Triage ACME, 900."}]},
        )
        assert chat.status_code == 200, chat.text
        catalog = json.loads(_tool_result(app.spy.prompts[1]))
        assert next(s for s in catalog if s["name"] == NAME)["source"] == "library"
        activated = json.loads(_tool_result(app.spy.prompts[2]))
        assert "Route amounts over 500 to the finance queue." in activated["instructions"]


async def test_an_advisory_import_saves_but_its_publish_is_refused(boot: Any, gh: FakeRepos) -> None:
    risky = BODY + "\nThe router binary is at https://example.test/router.sh if you need it.\n"
    gh.push("acme/skills", {f"skills/{NAME}/SKILL.md": skill_md(NAME, "Route incoming invoices.", risky)})
    async with boot([], env=ENV) as app:
        imported = await app.client.post("/skill-library/-/import", json={"source": SOURCE, "publish": True})
        assert imported.status_code == 201, imported.text
        row = imported.json()
        assert (row["status"], row["security_status"], row["published"]) == ("draft", "advisory", False)
        assert row["publish_blocked"]["error"] == "publish_blocked"
        assert (await app.client.get("/skill-library/-/review")).json()["items"][0]["name"] == NAME


async def test_refusals_carry_stable_codes(boot: Any, gh: FakeRepos) -> None:
    from felix.skills import library

    async with boot([], env=ENV) as app:

        async def code_of(source: str, **extra: Any) -> tuple[int, str]:
            resp = await app.client.post("/skill-library/-/import", json={"source": source, **extra})
            return resp.status_code, resp.json().get("error", "")

        assert await code_of("https://github.com/acme/skills") == (422, "invalid_source")
        assert await code_of("github:acme/skills/x", ref="../main") == (422, "invalid_source")
        assert await code_of("github:elsewhere/skills/x") == (403, "source_not_allowed")
        assert await code_of("github:acme/missing/skills/x") == (404, "source_not_found")
        assert await code_of("github:acme/skills/skills/nope") == (404, "source_not_found")

        await library.save_draft(
            app.settings,
            "default",
            files={"SKILL.md": skill_md("refunds", "Issue refunds.").decode()},
            provenance=library.DraftProvenance(source="operator", author="ops"),
        )
        assert await code_of("github:acme/skills/plugins/billing/skills/refunds") == (409, "origin_mismatch")

        gh.rate_limited = True
        assert await code_of(SOURCE) == (502, "upstream_rate_limited")
        browse = await app.client.get("/skill-library/-/browse", params={"source": "github:acme/skills"})
        assert (browse.status_code, browse.json()["error"]) == (502, "upstream_rate_limited")


async def test_a_skill_inside_the_cooldown_is_refused_over_http(boot: Any, gh: FakeRepos) -> None:
    import time

    fresh = {f"skills/{NAME}/SKILL.md": skill_md(NAME, "Route incoming invoices.", BODY)}
    gh.push("acme/fresh", fresh, at=int(time.time() * 1000) - 60_000)
    async with boot([], env={**ENV, "FELIX_SKILL_IMPORT_MIN_AGE_DAYS": "7"}) as app:
        refused = await app.client.post(
            "/skill-library/-/import", json={"source": f"github:acme/fresh/skills/{NAME}"}
        )
        assert (refused.status_code, refused.json()["error"]) == (403, "too_recent")
        assert "can be imported from" in refused.json()["message"]
        assert (await app.client.get(f"/skill-library/{NAME}")).status_code == 404

        browsed = await app.client.get("/skill-library/-/browse", params={"source": "github:acme/skills"})
        listing = browsed.json()
        assert listing["min_age_days"] == 7 and all(i["eligible"] for i in listing["items"])
        imported = await app.client.post("/skill-library/-/import", json={"source": SOURCE})
        assert imported.status_code == 201, imported.text
        assert imported.json()["origin_committed_at"] == listing["items"][0]["committed_at"]


async def test_browse_needs_skills_read_and_import_needs_skills_write(boot: Any, gh: FakeRepos) -> None:
    keys = {
        "sk-e2e-none": {"tenant_id": "default", "sub": "nobody", "scopes": ["chat:write"]},
        "sk-e2e-reader": {"tenant_id": "default", "sub": "reader", "scopes": ["skills:read"]},
        "sk-e2e-writer": {"tenant_id": "default", "sub": "writer", "scopes": ["skills:write"]},
    }
    env = {**ENV, "FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(keys)}

    def bearer(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer sk-e2e-{key}"}

    async with boot([], env=env) as app:
        browse = {"params": {"source": "github:acme/skills"}}
        assert (
            await app.client.get("/skill-library/-/browse", headers=bearer("none"), **browse)
        ).status_code == 403
        assert (
            await app.client.get("/skill-library/-/browse", headers=bearer("reader"), **browse)
        ).status_code == 200
        requests = len(gh.requests)
        body = {"json": {"source": SOURCE}}
        refused = await app.client.post("/skill-library/-/import", headers=bearer("reader"), **body)
        assert refused.status_code == 403 and "skills:write" in refused.text
        assert len(gh.requests) == requests, "a refused import never reached GitHub"
        done = await app.client.post("/skill-library/-/import", headers=bearer("writer"), **body)
        assert done.status_code == 201, done.text
        assert done.json()["author"] == "writer"
