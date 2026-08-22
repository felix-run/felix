"""Seed demo tenants / jobs / eval dataset for local Felix."""

from __future__ import annotations

import asyncio


async def main() -> None:
    from felix.config import get_settings
    from felix.eval import store as eval_store
    from felix.jobs import store as jobs_store
    from felix.manifests.loader import load_bundled
    from felix.manifests import store as manifest_store

    settings = get_settings()
    # Prefer memory when no Postgres is up during local seed demos.
    if "localhost" in settings.database_url or "127.0.0.1" in settings.database_url:
        # keep configured URL — operator may have compose up
        pass

    tenant = "default"
    print(f"seeding tenant={tenant} db={settings.database_url.split('@')[-1]}")

    # Bundle manifests into tenant store (version 1).
    for name in ("quick", "deep", "router"):
        try:
            m = load_bundled(name)
        except FileNotFoundError:
            continue
        row = await manifest_store.put_version(
            settings, tenant, name, m, created_by="seed", comment="seed"
        )
        print(f"  manifest {name}@{row['version']}")

    await jobs_store.put_job(
        settings,
        tenant,
        "heartbeat",
        schedule="*/15 * * * *",
        manifest_id="quick",
        payload={"prompt": "Reply with ok"},
        enabled=True,
    )
    print("  job heartbeat → quick")

    await eval_store.put_dataset(
        settings,
        tenant,
        "smoke",
        description="Seed smoke dataset",
        items=[
            {
                "item_id": "calc",
                "user_input": "What is 2+2? Reply with just the number.",
                "rubric": {"contains": "4"},
            }
        ],
    )
    print("  eval dataset smoke (1 item)")
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
