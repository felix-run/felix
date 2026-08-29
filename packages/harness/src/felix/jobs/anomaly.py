"""Tenant usage anomaly scan — volume spikes vs recent baseline."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from felix.audit import store as audit_store
from felix.config import Settings
from felix.db.session import _use_memory

logger = logging.getLogger("felix.jobs.anomaly")

now_ms = lambda: int(time.time() * 1000)

# Defaults matching manifest AnomalySpec floors.
MIN_VOLUME = 10
BASELINE_FACTOR = 3.0
WINDOW_MS = 60 * 60 * 1000  # 1h current
BASELINE_MS = 24 * 60 * 60 * 1000  # 24h baseline


async def _spec_for(settings: Settings, tenant_id: str, manifest_id: str) -> Any | None:
    """The manifest's AnomalySpec, or None when it cannot be resolved."""
    if not manifest_id or manifest_id == "_":
        return None
    try:
        from felix.runtime import resolve_tenant_manifest

        resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id)
        return resolved.manifest.spec.anomaly
    except ImportError:
        # Would silently return the module defaults for every manifest, i.e. make
        # spec.anomaly inert again — exactly the bug this change fixes.
        logger.error("anomaly spec lookup unavailable; using module defaults", exc_info=True)
        return None
    except Exception:
        logger.debug("anomaly spec lookup failed for %s", manifest_id, exc_info=True)
        return None


async def run_anomaly_scan(settings: Settings, *, tenant_id: str = "default") -> list[dict]:
    """Flag manifests whose recent audit volume exceeds baseline * factor.

    Thresholds come from each manifest's ``spec.anomaly``; the hardcoded module-level
    defaults are the fallback when a manifest cannot be resolved. The spec was
    previously ignored entirely, so `enabled: false` did not disable anything and
    per-manifest thresholds had no effect.
    """
    ts = now_ms()
    events, _ = await audit_store.list_events(settings, tenant_id, limit=500, cursor=None)
    if not events and _use_memory(settings):
        return []

    current_start = ts - WINDOW_MS
    baseline_start = ts - BASELINE_MS

    current: dict[str, int] = defaultdict(int)
    baseline: dict[str, int] = defaultdict(int)
    for ev in events:
        key = str(ev.get("manifest_id") or "_")
        ev_ts = int(ev.get("ts") or 0)
        if ev_ts >= current_start:
            current[key] += 1
        if baseline_start <= ev_ts < current_start:
            baseline[key] += 1

    findings: list[dict] = []
    for key, cur in current.items():
        spec = await _spec_for(settings, tenant_id, key)
        if spec is not None and not getattr(spec, "enabled", True):
            continue  # the manifest opted out
        min_volume = int(getattr(spec, "min_volume", MIN_VOLUME) if spec else MIN_VOLUME)
        factor = float(getattr(spec, "baseline_factor", BASELINE_FACTOR) if spec else BASELINE_FACTOR)

        base = baseline.get(key, 0)
        # Scale baseline to one-hour equivalent (24 windows).
        hourly_base = base / 24.0 if base else 0.0
        if cur < min_volume:
            continue
        if hourly_base <= 0:
            continue
        if cur >= hourly_base * factor:
            finding = {
                "tenant_id": tenant_id,
                "manifest_id": key,
                "current_volume": cur,
                "baseline_hourly": round(hourly_base, 2),
                "factor": round(cur / hourly_base, 2),
                "threshold_factor": factor,
                "min_volume": min_volume,
            }
            findings.append(finding)
            logger.warning("anomaly_detected %s", finding)

    return findings


__all__ = ["run_anomaly_scan"]


async def run_anomaly_scan_all_tenants(settings: Settings) -> list[dict]:
    """Scan every tenant that has audit events. Returns the combined findings.

    `run_anomaly_scan` defaults to ``tenant_id="default"`` and the worker cron never
    passed one, so on a multi-tenant deployment this detection control silently
    covered a single tenant. Same fix, and same shape, as `run_due_jobs_all_tenants`.
    """
    from felix.audit.store import list_tenants_with_events

    findings: list[dict] = []
    for tenant_id in await list_tenants_with_events(settings):
        try:
            findings.extend(await run_anomaly_scan(settings, tenant_id=tenant_id))
        except Exception:
            # One tenant's bad data must not stop the scan for everyone else.
            logger.exception("anomaly_scan_failed tenant=%s", tenant_id)
    return findings
