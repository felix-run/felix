"""Model catalog metadata for OpenAI-compatible listing."""

from __future__ import annotations

from typing import Any

from felix.model_catalog import entry_for
from felix.session.thinking import THINKING_LEVELS
from felix.usage.pricing import _lookup_price


def context_window_for(model_id: str | None, *, override: int | None = None) -> int:
    """Context window for a model id, from the catalog unless the manifest overrides it."""
    if override is not None and override > 0:
        return int(override)
    return entry_for(model_id).context_window


def supported_thinking_levels(model_id: str | None = None) -> list[str]:
    """Thinking levels the model accepts; `["off"]` when it supports none.

    The catalog records only *whether* a model thinks; the level vocabulary lives here
    because `felix.session.thinking` cannot be imported from the catalog without cycling
    back through the pattern layer.
    """
    return list(THINKING_LEVELS) if entry_for(model_id).supports_thinking else ["off"]


def modalities_for(model_id: str | None = None) -> list[str]:
    """Input modalities the model accepts."""
    return list(entry_for(model_id).input_modalities)


def model_catalog_entry(
    *,
    model_id: str,
    owned_by: str = "felix",
    context_window: int | None = None,
    price: dict[str, float] | None = None,
    modalities: list[str] | None = None,
    created: int = 0,
) -> dict[str, Any]:
    """Build an OpenAI-shaped model object with Felix catalog extensions."""
    prices = price or _lookup_price(model_id)
    return {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": owned_by,
        "felix": {
            "contextWindow": context_window_for(model_id, override=context_window),
            "cost": {
                "inputPerMillion": float(prices.get("input") or 0),
                "outputPerMillion": float(prices.get("output") or 0),
                "cacheReadPerMillion": float(prices.get("cache_read") or 0),
                "cacheWritePerMillion": float(prices.get("cache_write") or prices.get("input") or 0),
            },
            "modalities": modalities or modalities_for(model_id),
            "supportedThinkingLevels": supported_thinking_levels(model_id),
        },
    }


def _served_model(manifest: Any, settings: Any) -> tuple[str, str, Any]:
    """The model a manifest runs on: its route name, the id the catalog knows it by, and the route.

    A manifest without `spec.model.id` runs on `settings.default_model_id`, the fallback
    `patterns.model` takes when it builds the client; a route name resolves through
    `FELIX_MODEL_ROUTES` to the provider's model, which is what the catalog is keyed on.
    """
    from felix.patterns.model import parse_model_routes

    spec = getattr(getattr(manifest, "spec", None), "model", None)
    route_name = str(getattr(spec, "id", "") or "") or str(getattr(settings, "default_model_id", "") or "")
    route = parse_model_routes(settings).get(route_name) if route_name else None
    return route_name, (route.model if route is not None else route_name), route


def _served_modalities(manifest: Any, settings: Any, catalog_id: str, route: Any) -> list[str]:
    """What the manifest accepts, decided the way routing decides it.

    The route's own `modalities` win over the catalog (`route_accepts_images`), and a manifest
    whose primary cannot see images but has a vision route composed on (`vision_plan`) accepts
    them all the same — the image goes to the vision model.
    """
    from felix.patterns.model_vision import route_accepts_images, vision_plan

    declared = list(getattr(route, "modalities", None) or [])
    modalities = declared or modalities_for(catalog_id)
    sees = route_accepts_images(route)
    spec = getattr(getattr(manifest, "spec", None), "model", None)
    if "image" not in modalities and (sees or vision_plan(settings, spec).vision_id):
        modalities = [*modalities, "image"]
    return modalities


def catalog_from_manifest(
    name: str, manifest: Any | None = None, settings: Any | None = None
) -> dict[str, Any]:
    """An OpenAI model object for a manifest, described by the model it actually runs on.

    The `id` stays the manifest's name (the Felix convention); everything about the model —
    `providerModel`, window, price, modalities, thinking levels — comes from the model the
    manifest resolves to. Each used to be looked up by the manifest's *name*, which the catalog
    does not know, so a manifest on the default route listed no model and the fallback 128K
    window, text-only input and no thinking, whatever it ran on (#342). With no manifest (it
    failed to resolve) only the name is left to go on.
    """
    if manifest is None:
        entry = model_catalog_entry(model_id=name)
        entry["felix"].update(
            providerModel=None, description=None, starters=[], greeting=None, workspace=None
        )
        return entry
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    meta = getattr(manifest, "metadata", None)
    declared = getattr(meta, "greeting", None)
    # The window compaction uses: the declared value, else the served model's own.
    from felix.runtime import _context_window_for_manifest

    session = getattr(getattr(manifest, "spec", None), "session", None)
    context_window = _context_window_for_manifest(manifest, session, settings)
    route_name, catalog_id, route = _served_model(manifest, settings)
    raw_price = getattr(getattr(getattr(manifest, "spec", None), "model", None), "price", None)
    # Merged over the catalog's rates, as metering merges it: a manifest overriding only the
    # input rate is billed at the catalog's output rate, and listed that way.
    price = {**_lookup_price(catalog_id or name), **raw_price} if isinstance(raw_price, dict) else None
    entry = model_catalog_entry(
        model_id=catalog_id or name,
        context_window=context_window,
        price=price,
        modalities=_served_modalities(manifest, settings, catalog_id or name, route),
    )
    # Built under the served model's id so every lookup reads that model; the listing's id is
    # still the manifest's name, which is what a client sends back as `model`.
    entry["id"] = name
    entry["felix"]["providerModel"] = route_name or None
    entry["felix"]["description"] = getattr(meta, "description", "") or None
    # Always a list, empty when the manifest declares none, so a client can tell "this
    # harness lists starters" from an older one that has no key.
    entry["felix"]["starters"] = [
        {"title": s.title, "prompt": s.prompt} for s in (getattr(meta, "starters", None) or [])
    ]
    # `null` when the manifest declares none: the client's own greeting stands.
    entry["felix"]["greeting"] = (
        {"headline": declared.headline, "subtitle": declared.subtitle} if declared is not None else None
    )
    entry["felix"]["workspace"] = workspace_summary(manifest)
    return entry


# A client tool works in the user's own folder when its name says so: the `local_*` family the
# browser and terminal clients answer (`manifests/cowork.yaml`).
CLIENT_WORKSPACE_PREFIX = "local_"


def workspace_summary(manifest: Any) -> dict[str, str]:
    """Where this manifest's agent keeps files, for a client deciding which file pane to show.

    `tools`: `server` when it binds the harness's workspace tools (`list_dir`, `read_file`,
    `write_file`, `edit_file`, `search_files`) or a `shell_tools` command, which runs in the same
    directory; `client` when it binds `local_*` client tools, which the connected client answers
    from the user's own folder; `both`; or `none`. `scope` is `spec.workspace.scope`, the directory
    the server-side half works in (`GET /chat/workspace/tree` lists it). Read from the manifest's own
    declarations, not its sub-agents', which keep workspaces of their own.
    """
    from felix.tools.workspace import WORKSPACE_TOOL_NAMES

    spec = getattr(manifest, "spec", None)
    names = {str(t) for t in (getattr(spec, "tools", None) or [])}
    server = bool(names & WORKSPACE_TOOL_NAMES) or bool(getattr(spec, "shell_tools", None))
    client = any(
        str(getattr(ref, "name", "")).startswith(CLIENT_WORKSPACE_PREFIX)
        for ref in (getattr(spec, "client_tools", None) or [])
    )
    tools = "both" if server and client else "server" if server else "client" if client else "none"
    workspace = getattr(spec, "workspace", None)
    return {"tools": tools, "scope": str(getattr(workspace, "scope", None) or "thread")}


__all__ = [
    "catalog_from_manifest",
    "context_window_for",
    "modalities_for",
    "model_catalog_entry",
    "supported_thinking_levels",
    "workspace_summary",
]
