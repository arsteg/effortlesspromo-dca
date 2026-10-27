"""DCA-035 — the request-idempotency store.

One capability: make a tenant's *mutation request* safe to retry. The store reads and stages a
durable :class:`~app.db.models.IdempotencyRecord` — keyed by the caller's ``Idempotency-Key`` within
a single ``(business_id, method, route)`` — into the *caller's* session, so the record commits in the
SAME transaction as the business change it records (the same atomic-with-the-caller discipline as the
DCA-026 audit / DCA-027 outbox / DCA-028 inbox sinks).

The three outcomes the store models, given a key + a request-body digest:

- **New** (:meth:`lookup` returns ``None``): the request has not been seen; the caller runs the write
  and :meth:`remember` stages its outcome, committed atomically with the change.
- **Replay** (a record exists with the SAME digest): the caller returns the stored response verbatim
  and performs NO second state change — a retry is a no-op beyond echoing the first result.
- **Conflict** (a record exists with a DIFFERENT digest): the key was reused for a different body;
  :class:`IdempotencyConflict` is raised and the request is rejected, never served the first result.

``(business_id, method, route, idempotency_key)`` is unique, so the record that turns a retry into a
replay is the same row that makes a concurrent duplicate a constraint violation (the lock): the loser
rolls back its own duplicate change with the rejected record and then replays the winner's result.
Every lookup/insert is confined to the bound business, so one tenant's key can never match another's.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord

# The header a caller sets to make a write idempotent. Same spelling as the platform's BR-124 header.
IDEMPOTENCY_HEADER = "Idempotency-Key"

# A record may store at most this many bytes of response body JSON (a guard against a pathological
# payload; every DCA route response here is a handful of small control fields, far under this).
MAX_RESPONSE_BYTES = 64_000


class IdempotencyConflict(Exception):
    """An ``Idempotency-Key`` was reused for a request with a DIFFERENT body.

    Raised on lookup (before any write is attempted), so a rejected reuse changes no state. The HTTP
    layer maps it to a ``409`` — it is a client misuse of the key, not a validation error.
    """


def request_digest(body: bytes | None) -> str:
    """The SHA-256 (hex) of the request body bytes — the fingerprint a replay must match.

    Digesting the raw bytes (not a re-serialized JSON object) keeps it exact and dependency-free: a
    byte-identical retry replays; any change to the body yields a different digest and is treated as a
    key reuse. An empty/absent body (e.g. a POST with no payload) hashes deterministically too.
    """
    return hashlib.sha256(body or b"").hexdigest()


class IdempotencyStore:
    """Read/stage request-idempotency records for one tenant, bound to the caller's session.

    Construct with the active session and the verified business scope. The store never opens,
    commits, flushes, or rolls back a transaction — that is the caller's unit of work, which is
    exactly what makes the stored outcome atomic with the business change. It performs no I/O.
    """

    def __init__(self, session: Session, *, business_id: str | None = None) -> None:
        self._session = session
        self._business_id = business_id

    @classmethod
    def for_scope(cls, session: Session, business_id: str) -> IdempotencyStore:
        """Bind the store to a verified business scope. The scope is taken from the caller, never from
        a stored row, so a record can never be read or written across a tenant boundary."""
        return cls(session, business_id=business_id)

    def _scope(self, business_id: str | None) -> str:
        effective = business_id or self._business_id
        if not effective:
            raise ValueError("idempotency record requires a business scope")
        return effective

    def lookup(
        self, *, method: str, route: str, key: str, business_id: str | None = None
    ) -> IdempotencyRecord | None:
        """Return the prior record for this ``(method, route, key)`` within the tenant, or ``None``.

        Confined to the active business, so another tenant's key never matches. A caller compares the
        returned record's :attr:`~app.db.models.IdempotencyRecord.request_digest` to decide replay vs
        conflict, or use :meth:`reconcile` to get the decision directly.
        """
        effective = self._scope(business_id)
        method = (method or "").strip().upper()
        route = (route or "").strip()
        key = (key or "").strip()
        if not method or not route or not key:
            raise ValueError("idempotency lookup requires method, route and key")
        return (
            self._session.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.business_id == effective,
                IdempotencyRecord.method == method,
                IdempotencyRecord.route == route,
                IdempotencyRecord.idempotency_key == key,
            )
            .one_or_none()
        )

    def reconcile(
        self, *, method: str, route: str, key: str, digest: str, business_id: str | None = None
    ) -> IdempotencyRecord | None:
        """Decide this request's fate against any prior record with the same key.

        Returns the stored record to **replay** when one exists with a matching digest; returns
        ``None`` when the request is **new** (no prior record). Raises :class:`IdempotencyConflict`
        when a prior record exists with a DIFFERENT digest (the key was reused for a different body).
        """
        existing = self.lookup(method=method, route=route, key=key, business_id=business_id)
        if existing is None:
            return None
        if existing.request_digest != digest:
            raise IdempotencyConflict("Idempotency-Key reused with a different request body")
        return existing

    def remember(
        self,
        *,
        method: str,
        route: str,
        key: str,
        request_digest: str,
        response_status: int,
        response_body: dict | None = None,
        business_id: str | None = None,
    ) -> IdempotencyRecord:
        """Stage one idempotency record (the stored outcome) into the caller's session.

        The row is NOT committed here: the caller commits it in the SAME unit of work as the business
        change, so a replay that loses the unique-constraint race rolls back its duplicate change with
        this record. A missing scope/method/route/key is a programming error (``ValueError``) — the
        store is fail-closed. ``response_body`` must hold only opaque control fields (ids/statuses/
        flags) of the route's own response; it is replayed verbatim.
        """
        effective = self._scope(business_id)
        method = (method or "").strip().upper()
        route = (route or "").strip()
        key = (key or "").strip()
        digest = (request_digest or "").strip()
        if not method or not route or not key:
            raise ValueError("idempotency record requires method, route and key")
        if not digest:
            raise ValueError("idempotency record requires a request digest")
        record = IdempotencyRecord(
            business_id=effective,
            idempotency_key=key,
            method=method,
            route=route,
            request_digest=digest,
            response_status=int(response_status),
            response_body=dict(response_body or {}),
        )
        self._session.add(record)
        return record
