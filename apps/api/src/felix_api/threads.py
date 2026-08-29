"""Thread-id scoping for the HTTP layer.

Clients send a thread-id *suffix*; the server prefixes the tenant so a thread can
never be addressed across tenants. This is a security rule, so it lives in one
place rather than being restated per router.
"""

from __future__ import annotations

# A suffix carrying either delimiter could forge a tenant prefix or a reserved
# thread namespace, so it is rejected rather than escaped.
SUFFIX_DELIMS = frozenset(":#")

# One cap, applied by both helpers. `thread_id` is Text, part of the session-events
# primary key and its index, and is interpolated into advisory-lock keys and Redis
# channel names — an unbounded id is index bloat and an oversized lock key, repeatable
# at the rate limit.
MAX_THREAD_ID = 512


def _usable_tenant(tenant_id: str) -> bool:
    """A tenant id must be a single delimiter-free segment.

    The tenant prefix is the whole ownership boundary, so a tenant id carrying the
    delimiter stops it partitioning: `acme` and `acme:sub` would both "own" the
    thread `acme:sub:x`. Rows still land under the caller's own tenant, so it is not
    a cross-tenant write — but `session/lease.py` keys a lease by thread id alone,
    which is only safe while that prefix is unambiguous.
    """
    return bool(tenant_id) and not any(c in tenant_id for c in SUFFIX_DELIMS)


def effective_thread_id(tenant_id: str, suffix: str | None) -> str | None:
    """Return the tenant-scoped thread id, or None if the suffix is unusable.

    None means either "no thread requested" (empty suffix) or "malformed" — the
    caller distinguishes them by whether it passed a suffix at all, and answers
    400 ``invalid_thread_id`` in the malformed case.
    """
    if not suffix:
        return None
    if not _usable_tenant(tenant_id):
        return None
    if any(c in suffix for c in SUFFIX_DELIMS):
        return None
    thread_id = f"{tenant_id}:{suffix}"
    return thread_id if len(thread_id) <= MAX_THREAD_ID else None


def thread_belongs_to_tenant(tenant_id: str, thread_id: str) -> bool:
    """True when ``thread_id`` is one ``tenant_id`` may address.

    The counterpart to `effective_thread_id` for ids that arrive already built —
    `/internal` takes one from a queue write-back envelope rather than composing it
    from a client suffix, so it needs to check ownership rather than construct it.

    The rule is prefix ownership, not a delimiter-free suffix: fibers legitimately
    mint ``{tenant}:fiber:{id}``. That is safe because thread ids are compared whole,
    so ``acme:default:x`` is a different id from ``default:x`` rather than a way to
    reach it. A tenant id carrying the delimiter *would* make the split ambiguous,
    so it is refused outright.
    """
    if not _usable_tenant(tenant_id) or not thread_id:
        return False
    if len(thread_id) > MAX_THREAD_ID or "#" in thread_id:
        # `#` is rejected for the same reason `effective_thread_id` rejects it in a
        # suffix. Accepting it here let `/internal` mint ids no chat route could ever
        # address, read, export or delete.
        return False
    prefix = f"{tenant_id}:"
    return thread_id.startswith(prefix) and len(thread_id) > len(prefix)
