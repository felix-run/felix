"""No credential survives a `repr`.

`Settings` rendered provider keys, connection passwords and signing secrets in clear;
`RequestContext` carries `Settings` on every request, and the model client kept its key as a plain
dataclass field. Nothing logged any of them — so this was one `logger.debug("%r", ctx)` or one
traceback's locals away from a leak, not a live one. Each credential field is `repr=False` now,
and the guard below makes a *new* one fail closed: a field whose name looks like a credential
must be hidden, or be listed here as not one.
"""

from __future__ import annotations

import re

from felix.config import Settings

LOOKS_SECRET = re.compile(r"key|secret|token|password|passwd|private|credential|_url$|options$|endpoints$")

# Names that match the pattern and hold nothing secret — each with why.
NOT_SECRET = {
    "litellm_base_url": "an endpoint; its key travels in model_provider_options",
    "search_url": "an endpoint; its key is search_api_key",
    "policy_bundle_pubkey": "a public key",
    "secret_names": "names of secrets, not their values",
    "secrets_backend": "a backend name",
    "secrets_dir": "a path",
    "skill_eval_max_tokens": "a count of model output tokens",
    "skill_improve_max_tokens": "a count of model output tokens",
}

SENTINEL = "SENTINEL-do-not-print"


def _credential_fields() -> list[str]:
    return [name for name in Settings.model_fields if LOOKS_SECRET.search(name) and name not in NOT_SECRET]


def test_every_credential_field_is_out_of_repr() -> None:
    printable = [name for name in _credential_fields() if Settings.model_fields[name].repr]
    assert printable == [], f"credential-looking Settings fields still in repr: {printable}"
    assert len(_credential_fields()) >= 10, "the pattern stopped matching the fields it exists for"
    stale = [n for n in NOT_SECRET if n not in Settings.model_fields or not LOOKS_SECRET.search(n)]
    assert stale == [], f"NOT_SECRET entries that no longer need excusing: {stale}"


def test_no_credential_value_reaches_a_repr() -> None:
    from felix.context import AuthContext, RequestContext

    values = {name: f"{SENTINEL}-{name}" for name in _credential_fields()}
    values.update(database_url=f"postgresql+psycopg://u:{SENTINEL}@h/d", model_provider_options="{}")
    settings = Settings(**values)
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="t", principal_sub="p"))
    assert SENTINEL not in repr(settings)
    assert SENTINEL not in repr(ctx), "the context carries Settings on every request"


def test_the_model_client_keeps_its_key_out_of_repr() -> None:
    from felix.manifests.schema import ModelSpec
    from felix.patterns.model import build_model

    settings = Settings(database_url="memory://repr", anthropic_api_key=f"sk-ant-{SENTINEL}")
    client = build_model(settings, ModelSpec(id="claude-sonnet"))
    assert client.api_key.endswith(SENTINEL), "the key is still there to use"
    assert SENTINEL not in repr(client)
