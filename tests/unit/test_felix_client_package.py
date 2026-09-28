"""`felix-client` installs and imports without the server.

The client lived inside the harness, so `from felix.sdk import FelixClient` imported FastAPI,
SQLAlchemy, psycopg and the rest of the server's tree. It is its own package now, depending on
httpx alone; `test_invariants.py` keeps it from importing Felix, and this checks what that
buys — a fresh interpreter that imports the client loads none of the server.
"""

from __future__ import annotations

import json
import subprocess
import sys

SERVER_SIDE = {
    "felix",
    "felix_ai",
    "felix_api",
    "felix_worker",
    "fastapi",
    "sqlalchemy",
    "psycopg",
    "pydantic",
}


def test_importing_the_client_loads_none_of_the_server() -> None:
    probe = (
        "import json, sys, felix_client; "
        f"print(json.dumps(sorted({{m.split('.')[0] for m in sys.modules}} & set({sorted(SERVER_SIDE)!r}))))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [], out.stdout


def test_the_old_import_path_still_works() -> None:
    import felix_client
    from felix.sdk import FelixClient

    assert FelixClient is felix_client.FelixClient
