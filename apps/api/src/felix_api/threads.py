"""Thread-id scoping for the HTTP layer.

Clients send a thread-id *suffix*; the server prefixes the tenant so a thread can
never be addressed across tenants. This is a security rule, so it lives in one
place rather than being restated per router.
"""

from __future__ import annotations

# A suffix carrying either delimiter could forge a tenant prefix or a reserved
# thread namespace, so it is rejected rather than escaped.
SUFFIX_DELIMS = frozenset(":#")


def effective_thread_id(tenant_id: str, suffix: str | None) -> str | None:
    """Return the tenant-scoped thread id, or None if the suffix is unusable.

    None means either "no thread requested" (empty suffix) or "malformed" — the
    caller distinguishes them by whether it passed a suffix at all, and answers
    400 ``invalid_thread_id`` in the malformed case.
    """
    if not suffix:
        return None
    if any(c in suffix for c in SUFFIX_DELIMS):
        return None
    return f"{tenant_id}:{suffix}"
