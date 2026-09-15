"""Thread-id scoping.

Clients send a thread-id *suffix*; the server prefixes the tenant so a thread can
never be addressed across tenants. This is a security rule, so it lives in one
place rather than being restated per router.

It sat in `felix_api` while the HTTP routes were its only callers. They are not:
the harness mints thread ids of its own (`a2a`, `eval`, `fiber`) from parts that
are just as caller-supplied, and it cannot import the app. One definition or two
that disagree — this is the first, so the module moved down rather than the rule
being restated.
"""

from __future__ import annotations

from felix.auth.context import assert_valid_tenant_id

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
    # One definition of the rule. It is now enforced at issuance too — an unusable tenant
    # cannot reach a `Principal` — so this is the second line of defence rather than the
    # only one, and it must not be able to disagree with the first.
    try:
        assert_valid_tenant_id(tenant_id)
    except ValueError:
        return False
    return True


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


def _compose(tenant_id: str, *segments: str, tail: str) -> str | None:
    """Back the named composers below: ``{tenant}:{segments…}:{tail}``.

    `effective_thread_id` is the client-facing composer and takes one delimiter-free
    suffix. This is its counterpart for the namespaces the harness mints with segments of
    its own — ``{tenant}:a2a:{task_id}``, ``{tenant}:eval:{run}:{item}`` — where a segment
    is still caller-supplied and was being interpolated with a bare f-string, reaching
    none of the checks every client-supplied suffix passes.

    Returns None when the result would not be a thread id the rest of the system can
    address, which each caller answers for in its own protocol. The property that matters,
    and the one the tests pin: **anything this returns satisfies
    `thread_belongs_to_tenant`**. An id that does not is one `/internal` refuses and no
    operator can address — the failure `#` produced.

    `tail` may contain `:` and no earlier segment may, which is what keeps the composition
    injective: the delimiter-free segments fix each boundary left to right, and the tail is
    the whole remainder either way. Rejecting it outright would break the `urn:uuid:…` task
    ids several A2A clients send, and buys nothing. `#` is rejected everywhere, matching
    `thread_belongs_to_tenant`.

    Private, and reached only through one composer per namespace, because *which* segment
    is permissive must not be a function of how many arguments a call site happens to pass.
    With a variadic tail, adding one fixed trailing segment to the A2A id would silently
    move `task_id` out of the permissive slot and start refusing the very ids the paragraph
    above exists to keep working. A signature is a cheaper guard than that sentence.
    """
    if not _usable_tenant(tenant_id):
        return None
    if not tail or not all(segments):
        return None
    if any(c in seg for seg in segments for c in SUFFIX_DELIMS):
        return None
    if "#" in tail:
        return None
    thread_id = ":".join((tenant_id, *segments, tail))
    return thread_id if len(thread_id) <= MAX_THREAD_ID else None


def a2a_thread_id(tenant_id: str, task_id: str) -> str | None:
    """The thread an A2A `message/send` runs on. `task_id` comes off the wire."""
    return _compose(tenant_id, "a2a", tail=task_id)


def eval_thread_id(tenant_id: str, run_id: str, item_id: str) -> str | None:
    """The thread one eval item runs on.

    `item_id` is dataset content, so caller-supplied through `PUT /eval/datasets/{name}`;
    `run_id` is the `uuid4().hex` minted by `eval/store.py:create_run`.
    """
    return _compose(tenant_id, "eval", run_id, tail=item_id)


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


# Named now that this is imported across workspace members: `_usable_tenant` is the second
# line of defence behind `assert_valid_tenant_id` and not part of the contract, and
# `_compose` is private on purpose (see its docstring).
__all__ = [
    "MAX_THREAD_ID",
    "SUFFIX_DELIMS",
    "a2a_thread_id",
    "effective_thread_id",
    "eval_thread_id",
    "thread_belongs_to_tenant",
]
