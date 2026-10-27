"""DCA-035 — request idempotency.

Make a tenant's mutation request safe to retry: the first call runs the write and stores its outcome
keyed by the caller's ``Idempotency-Key`` within one ``(business_id, method, route)``; a byte-for-byte
retry replays the stored result with no second state change; the same key with a different body is
rejected; and keys never collide across tenants or routes.

- :class:`~app.idempotency.store.IdempotencyStore` is the tenant-scoped, caller-atomic writer/reader
  of :class:`~app.db.models.IdempotencyRecord`.
- :mod:`app.api.idempotency` wires it into the service's write routes (the HTTP seam: a dependency
  that replays/rejects before the handler, and a helper that commits the record atomically with the
  change).
"""

from app.idempotency.store import (
    IDEMPOTENCY_HEADER,
    IdempotencyConflict,
    IdempotencyStore,
    request_digest,
)

__all__ = [
    "IDEMPOTENCY_HEADER",
    "IdempotencyConflict",
    "IdempotencyStore",
    "request_digest",
]
