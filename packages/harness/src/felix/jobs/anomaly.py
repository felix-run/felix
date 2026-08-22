"""Tenant usage anomaly scan — volume spikes vs recent baseline."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

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


async def run_anomaly_scan(settings: Settings, *, tenant_id: str = "default") -> list[dict]:
    """Flag tenants/manifests whose recent audit volume exceeds baseline * factor."""
    ts = now_ms()
    events, _ = await audit_store.list_events(
        settings, tenant_id, limit=500, cursor=None
    )
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
        base = baseline.get(key, 0)
        # Scale baseline to one-hour equivalent (24 windows).
        hourly_base = base / 24.0 if base else 0.0
        if cur < MIN_VOLUME:
            continue
        if hourly_base <= 0:
            continue
        if cur >= hourly_base * BASELINE_FACTOR:
            finding = {
                "tenant_id": tenant_id,
                "manifest_id": key,
                "current_volume": cur,
                "baseline_hourly": round(hourly_base, 2),
                "factor": round(cur / hourly_base, 2),
            }
            findings.append(finding)
            logger.warning("anomaly_detected %s", finding)

    return findings


__all__ = ["run_anomaly_scan"]
