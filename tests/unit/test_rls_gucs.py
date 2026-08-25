"""What the `after_begin` listener actually sets, per configuration.

Migration `0006_tenant_rls` applies ENABLE *and* FORCE ROW LEVEL SECURITY
unconditionally, so on a migrated database the policy is live whatever
``FELIX_DATABASE_RLS`` says. The listener is therefore the only thing standing
between a deployment that has not opted in and a policy that filters everything:
with neither GUC set, `tenant_id = current_setting('app.tenant_id', true)` is
NULL, which is not true, and every row disappears — silently, on any role RLS
applies to.

These assert the GUC that gets set rather than going near a database. The
behaviour under a real policy is not in doubt; measured on a migrated database,
reading `thread_state` as a non-superuser role: no GUC → 0 rows,
`app.rls_bypass=on` → 25, `app.tenant_id` set → 25. What regresses is the
mapping from settings to GUC, which is what lives here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.db import session as sess


class _Conn:
    """Records the SQL the listener runs, with its bind parameters."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self.calls.append((str(statement), params or {}))


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


def _fire(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> _Conn:
    """Invoke the after_begin body directly, with `settings` in force."""
    monkeypatch.setattr(sess, "get_settings", lambda: settings)
    conn = _Conn()
    sess._rls_after_begin(_Session(), object(), conn)
    return conn


def _sql(conn: _Conn) -> str:
    return " ".join(sql for sql, _ in conn.calls)


def test_rls_off_declares_the_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this file exists for.

    Setting nothing here is what produced a total, silent blackout on a migrated
    database — the policy is FORCE'd, so it binds the table owner too.
    """
    conn = _fire(monkeypatch, Settings(database_rls=False))

    assert "app.rls_bypass" in _sql(conn)
    assert "app.tenant_id" not in _sql(conn)
    assert conn.calls, "RLS off must still declare intent, not set nothing"


def test_rls_on_sets_the_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    with sess.rls_tenant("acme"):
        conn = _fire(monkeypatch, Settings(database_rls=True))

    assert "app.tenant_id" in _sql(conn)
    assert conn.calls[0][1] == {"t": "acme"}
    assert "app.rls_bypass" not in _sql(conn)


def test_explicit_bypass_wins_when_rls_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-tenant admin paths — the fiber scheduler, memory maintenance."""
    with sess.rls_bypass(), sess.rls_tenant("acme"):
        conn = _fire(monkeypatch, Settings(database_rls=True))

    assert "app.rls_bypass" in _sql(conn)
    assert "app.tenant_id" not in _sql(conn)


def test_unresolvable_tenant_stays_filtered_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Deny is the only safe answer, but it must not be silent.

    RLS is the isolation mechanism here and we cannot say whose data this is, so
    setting a bypass would be a hole. Leaving the policy to filter is correct —
    and indistinguishable from an empty table without the warning.
    """
    with caplog.at_level("WARNING", logger="felix.db.session"):
        conn = _fire(monkeypatch, Settings(database_rls=True))

    assert conn.calls == [], "no GUC may be set for an unattributable transaction"
    assert any("no tenant could be resolved" in r.message for r in caplog.records)


def test_off_and_on_never_set_the_same_guc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two configurations must be distinguishable at the database.

    If `database_rls=False` ever set `app.tenant_id`, an opt-out deployment would
    silently start enforcing isolation it never asked for; if `True` set
    `app.rls_bypass`, an opt-in one would silently stop.
    """
    off = _sql(_fire(monkeypatch, Settings(database_rls=False)))
    with sess.rls_tenant("acme"):
        on = _sql(_fire(monkeypatch, Settings(database_rls=True)))

    assert off != on
    assert "rls_bypass" in off and "tenant_id" in on
