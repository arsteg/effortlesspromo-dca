"""DCA-127 — cache only entitled, fresh source results (the safe source-result cache).

:class:`SourceResultCache` is the single sanctioned reader/writer of
:class:`~app.db.models.SourceResultCacheEntry`. It lets ONE business cache the result of a source fetch
so a repeat request does not re-fetch — but it is built so a cache HIT can never become a shortcut
around the rights chain, the clock, or tenancy. The cache stores and serves PROVIDED results only; it
performs no fetch, so paid/live acquisition stays a disabled future capability.

The load-bearing guarantees (the DCA-127 acceptance criteria):

- **A hit can never exceed current entitlement or survive source revocation.** Entitlement is rechecked
  on the READ, never trusted from the WRITE: :meth:`SourceResultCache.get` re-runs the DCA-048
  :class:`~app.services.source_preflight.SourcePreflight` (source approved + ENABLED, the in-force
  platform ceiling at ``now``, the business's enabled entitlement, and the purpose) and a denial — a
  lost entitlement, a revoked/expired approval — is a MISS, never another result. The entry is pinned to
  the terms ``source_version`` it was cached under; a read resolves the CURRENT in-force terms version
  and only an entry cached under THAT version is eligible, so a newer approved terms version makes an old
  entry unreachable (a miss) rather than serving a result the current entitlement would not grant.

- **A stale/expired entry is a miss, never served; negative results expire.** ``fresh_until`` is the
  freshness deadline derived from the source's cited terms (its retention/freshness window), bounded by a
  configured maximum; a read at/after ``fresh_until`` is a MISS and the row is never returned. A NEGATIVE
  (no-result) entry carries a BOUNDED negative TTL and expires the same way, so a transient "nothing
  found" is never cached forever.

- **Writes respect retention (no-retention ⇒ only a ref/digest).** The retention mode is DERIVED from the
  cited terms at write time via the SAME screen as DCA-062 (:func:`~app.services.evidence_store.
  decide_retention`): where raw retention is prohibited the result ``payload`` is dropped (``NULL``,
  never written) while the locator ``result_ref`` and the non-reversible ``result_digest`` are kept — so
  even a hit on a no-retention source serves only a reference, never a prohibited raw copy.

- **Cross-entitlement isolation.** The cache is bound to ONE verified business scope; every read/write is
  confined to that business (DCA-017 scoping + DCA-018 RLS beneath it). One business can never read
  another's cached result — the same ``cache_key`` legitimately recurs across tenants, each bound to that
  tenant's own entitlement — and the rights recheck + terms-version pin are a second, in-tenant guard on
  top of RLS.

- **Idempotent put.** A re-put under the same ``(source_key, source_version, cache_key)`` finds the
  existing entry and is a deterministic no-op (no duplicate row, no second audit).

It stages into the caller's session and never commits — the caller owns the unit of work, so the cache
entry and its atomic DCA-026 audit entry commit together. The audit references stay OPAQUE (cache key /
source key+version / operation / retention mode+reason / the safe digest); the raw payload is NEVER
audited.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.core.config import Settings, get_settings
from app.db.models import (
    Source,
    SourceOperation,
    SourceResultCacheEntry,
    SourceTerms,
)
from app.services.evidence_store import decide_retention
from app.services.source_entitlement import BusinessSourceEntitlementStore
from app.services.source_preflight import PreflightRequest, SourcePreflight

# The audited action code for writing a source-result cache entry.
AUDIT_ACTION_PUT_CACHE = "source.result_cache.put"

# Validation refusals on a PUT — each raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_MISSING_SOURCE_KEY = "missing_source_key"
REASON_MISSING_SOURCE_VERSION = "missing_source_version"
REASON_UNKNOWN_OPERATION = "unknown_operation"
REASON_MISSING_PURPOSE = "missing_purpose"
REASON_MISSING_CACHE_KEY = "missing_cache_key"
REASON_MISSING_FETCHED_AT = "missing_fetched_at"
REASON_SOURCE_NOT_FOUND = "source_not_found"
REASON_SOURCE_VERSION_NOT_FOUND = "source_version_not_found"

# MISS reasons a consumer sees from a lookup (never a staging error). A rights-denial MISS passes through
# the DCA-048 preflight's own stable reason; these are the cache-specific misses.
REASON_NOT_CACHED = "not_cached"
REASON_STALE = "stale_or_expired"


class SourceResultCacheError(Exception):
    """A cache write was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no row and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class CacheResultInput:
    """One source-fetch result to cache, as supplied by the caller (the cache performs no fetch).

    ``source_key`` / ``source_version`` name the catalogued source + terms version the result was fetched
    under (REQUIRED — the result is bound to the entitlement/terms it was collected under). ``operation``
    is the SourceOperation it was fetched under (``collect`` by default) and ``field_key`` the specific
    field ("" = source-wide); both are rechecked on read. ``purpose`` is the purpose the fetch served
    (rights are purpose-bound). ``cache_key`` is the caller's opaque request fingerprint (the dedup/
    idempotency token). ``payload`` is the fetched result (kept only when the licence permits raw
    retention); ``negative`` marks a cached "no result". ``result_kind`` keys the per-kind retention
    override; ``result_ref`` is the locator kept in both modes; ``fetched_at`` is when it was fetched.
    ``fresh_seconds`` optionally requests a freshness window — always clamped to the terms' window and the
    configured maximum (positive) or the bounded negative TTL (negative), never widened past them.
    """

    source_key: str
    source_version: int
    purpose: str
    cache_key: str
    fetched_at: datetime
    operation: str = SourceOperation.COLLECT.value
    field_key: str = ""
    payload: dict | None = None
    negative: bool = False
    result_kind: str = ""
    result_ref: str = ""
    rights_basis: str = ""
    fresh_seconds: int | None = None


@dataclass(frozen=True)
class CacheLookup:
    """The deterministic outcome of a cache :meth:`SourceResultCache.get`.

    ``hit`` is the verdict. On a MISS ``hit`` is ``False``, ``reason`` is the stable code (a DCA-048
    preflight reason when entitlement/approval failed, :data:`REASON_NOT_CACHED` when nothing is stored
    for the current terms version, or :data:`REASON_STALE` when the entry has expired) and ``entry`` is
    ``None`` — nothing is served. On a HIT ``entry`` is the stored row: ``negative`` tells apart a cached
    "no result" from a positive one, ``payload`` is the retained result (``None`` for a reference-only or
    negative entry), and ``result_ref`` / ``result_digest`` are the locator + fingerprint kept in both
    modes. ``phase`` records which gate decided a miss (``rights`` / ``cache`` / ``freshness``).
    """

    hit: bool
    reason: str | None = None
    phase: str = ""
    entry: SourceResultCacheEntry | None = None

    @property
    def negative(self) -> bool:
        return bool(self.entry.negative) if self.entry is not None else False

    @property
    def payload(self) -> dict | None:
        return self.entry.result_payload if self.entry is not None else None

    @property
    def result_ref(self) -> str:
        return self.entry.result_ref if self.entry is not None else ""

    @property
    def result_digest(self) -> str:
        return self.entry.result_digest if self.entry is not None else ""

    @property
    def retention_mode(self) -> str:
        return self.entry.retention_mode if self.entry is not None else ""

    @property
    def source_version(self) -> int | None:
        return self.entry.source_version if self.entry is not None else None


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    return value


def _digest(payload: dict | None, result_ref: str) -> str:
    """A non-reversible SHA-256 (hex) fingerprint of the result — kept in BOTH modes so a cached result
    can be verified / matched without retaining the raw payload. Falls back to the locator for a negative
    (payload-less) entry so even a negative result carries a stable fingerprint."""
    if payload is not None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    else:
        raw = f"ref:{result_ref}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SourceResultCache:
    """Tenant-scoped reader/writer for cached source-fetch results, bound to one business.

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-048 rights preflight (rechecked on READ),
    the DCA-047 entitlement store (to resolve the current in-force terms version) and the DCA-062
    retention screen (applied on WRITE). Every query is confined to that business — there is no argument
    to widen the scope.
    """

    def __init__(
        self, session: Session, business_id: str, *, settings: Settings | None = None
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._settings = settings or get_settings()
        self._preflight = SourcePreflight(session, business_id)
        self._entitlements = BusinessSourceEntitlementStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def entry_for(
        self, source_key: str, source_version: int, cache_key: str
    ) -> SourceResultCacheEntry | None:
        """The cache row for one logical request + terms version in this business, or ``None``."""
        stmt = (
            select(SourceResultCacheEntry)
            .where(SourceResultCacheEntry.business_id == self._business_id)
            .where(SourceResultCacheEntry.source_key == (source_key or "").strip())
            .where(SourceResultCacheEntry.source_version == source_version)
            .where(SourceResultCacheEntry.cache_key == (cache_key or "").strip())
        )
        return self._session.scalars(stmt).one_or_none()

    def entries_for(self, source_key: str) -> list[SourceResultCacheEntry]:
        """Every cache entry recorded for one source in this business (oldest first)."""
        stmt = (
            select(SourceResultCacheEntry)
            .where(SourceResultCacheEntry.business_id == self._business_id)
            .where(SourceResultCacheEntry.source_key == (source_key or "").strip())
            .order_by(SourceResultCacheEntry.created_at, SourceResultCacheEntry.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one lookup (pure, side-effect-free: no adapter, no state change) -----------------------

    def get(
        self,
        source_key: str,
        operation: str,
        purpose: str,
        cache_key: str,
        *,
        field_key: str = "",
        now: datetime | None = None,
    ) -> CacheLookup:
        """Decide whether a fresh, entitled, same-terms-version result is cached; a pure, side-effect-free
        read (stages nothing, commits nothing, calls no adapter), so the decision is reproducible.

        The gates run in a FIXED order, stopping at the first failure:

        1. **Rights recheck (DCA-048).** The current entitlement must still permit ``operation`` on
           ``field_key`` for ``purpose`` against an in-force, approved+ENABLED source — a denial (lost
           entitlement / revoked approval) is a MISS (``phase='rights'``) carrying the preflight's reason.
        2. **Terms-version pin.** The read resolves the CURRENT in-force terms version; only an entry
           cached under THAT version is eligible, so a superseded entry is a MISS (``REASON_NOT_CACHED``).
        3. **Freshness.** A found entry at/after its ``fresh_until`` is a MISS (``REASON_STALE``) and is
           never returned — the same rule expires a bounded negative entry.

        Only when all three pass is the stored entry a HIT.
        """
        moment = now or datetime.now(UTC)

        # 1) Rights recheck on READ — a denial (lost entitlement / revoked or expired approval) is a miss,
        #    never a served result. ``origin_signals`` are irrelevant here; the rights chain decides.
        decision = self._preflight.authorize(
            PreflightRequest(
                source_key=source_key, operation=operation, purpose=purpose, field=field_key
            ),
            now=moment,
        )
        if not decision.allowed:
            return CacheLookup(hit=False, reason=decision.reason, phase="rights")

        # 2) Terms-version pin — resolve the CURRENT in-force approved terms version and look up ONLY an
        #    entry cached under it. A superseded (older/newer) version is unreachable, so a hit can never
        #    serve a result the current entitlement would not grant. (The preflight already proved an
        #    in-force approval exists; this is a belt-and-braces guard.)
        approval = self._entitlements.in_force_approval(source_key, now=moment)
        if approval is None:
            return CacheLookup(hit=False, reason=decision.reason, phase="rights")
        entry = self.entry_for(source_key, approval.terms_version, cache_key)
        if entry is None:
            return CacheLookup(hit=False, reason=REASON_NOT_CACHED, phase="cache")

        # 3) Freshness — an entry at/after its freshness deadline is STALE and is never served (this also
        #    expires a bounded negative entry).
        fresh_until = _as_utc(entry.fresh_until)
        if fresh_until is not None and moment >= fresh_until:
            return CacheLookup(hit=False, reason=REASON_STALE, phase="freshness")

        return CacheLookup(hit=True, entry=entry)

    # -- the one write path -------------------------------------------------------------------------

    def put(
        self,
        data: CacheResultInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> SourceResultCacheEntry:
        """Validate the provenance, apply the license-aware retention screen, bound the freshness window,
        and stage ONE cache entry; return the (uncommitted) row. The caller owns the unit of work, so the
        row and its atomic audit entry commit together.

        Every check runs before the session is mutated. A write is refused — and stages NO row — when it
        is missing its source reference / purpose / cache key / fetched-at, names an unrecognized
        operation, or cites a source/version that does not exist. The retention mode is DERIVED from the
        cited terms (:func:`~app.services.evidence_store.decide_retention`): where raw retention is
        prohibited the payload is dropped (``result_payload`` stays ``None``) and only the locator +
        digest are kept. ``fresh_until`` is bounded by the terms window and the configured maximum (or the
        bounded negative TTL). A re-put under the same ``(source_key, source_version, cache_key)`` is a
        deterministic no-op: the existing entry is returned unchanged (no duplicate, no second audit).
        """
        moment = now or datetime.now(UTC)

        # 1) Request shape — raised before any state is read.
        source_key = (data.source_key or "").strip()
        if not source_key:
            raise SourceResultCacheError(REASON_MISSING_SOURCE_KEY, "a source_key is required")
        if data.source_version is None:
            raise SourceResultCacheError(
                REASON_MISSING_SOURCE_VERSION, "a source_version is required"
            )
        try:
            operation = SourceOperation((data.operation or "").strip().lower()).value
        except ValueError as exc:
            raise SourceResultCacheError(
                REASON_UNKNOWN_OPERATION,
                f"unknown operation '{data.operation}'",
                detail=data.operation,
            ) from exc
        purpose = (data.purpose or "").strip()
        if not purpose:
            raise SourceResultCacheError(REASON_MISSING_PURPOSE, "a purpose is required")
        cache_key = (data.cache_key or "").strip()
        if not cache_key:
            raise SourceResultCacheError(
                REASON_MISSING_CACHE_KEY, "a cache_key (the request fingerprint) is required"
            )
        if data.fetched_at is None:
            raise SourceResultCacheError(
                REASON_MISSING_FETCHED_AT, "a fetched_at timestamp is required"
            )

        # 2) Provenance must point at a real, reviewable source + terms version.
        if not self._source_exists(source_key):
            raise SourceResultCacheError(
                REASON_SOURCE_NOT_FOUND,
                f"no catalogued source with key '{source_key}' exists",
                detail=source_key,
            )
        terms = self._source_terms(source_key, data.source_version)
        if terms is None:
            raise SourceResultCacheError(
                REASON_SOURCE_VERSION_NOT_FOUND,
                f"source '{source_key}' has no terms version {data.source_version}",
                detail=source_key,
            )

        # 3) Idempotency — a logical request already cached under this terms version is a no-op. Checked
        #    before anything is staged, so a replay writes no second row and no second audit.
        existing = self.entry_for(source_key, data.source_version, cache_key)
        if existing is not None:
            return existing

        field_key = (data.field_key or "").strip()
        result_kind = (data.result_kind or "").strip().lower()

        # 4) License-aware retention — DERIVED from the cited terms (the DCA-062 screen), never
        #    caller-chosen. A no-retention source keeps only the locator + digest, never the raw payload.
        #    A negative entry holds no payload regardless.
        retention = decide_retention(terms, result_kind)
        digest = _digest(data.payload, data.result_ref)
        stored_payload: dict | None
        if data.negative or not retention.raw_permitted:
            stored_payload = None
        else:
            stored_payload = data.payload

        # 5) Freshness window — bounded by the terms window and the configured maximum (positive) or the
        #    bounded negative TTL (negative). Never widened past them, so a result can never be served
        #    forever.
        fresh_seconds = self._freshness_seconds(
            terms, negative=data.negative, requested=data.fresh_seconds
        )
        fresh_until = data.fetched_at + timedelta(seconds=fresh_seconds)

        basis = (
            data.rights_basis or ""
        ).strip() or f"approved_source:{source_key}/{operation}/{purpose}"
        entry = SourceResultCacheEntry(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            source_key=source_key,
            source_version=data.source_version,
            operation=operation,
            field_key=field_key,
            purpose=purpose,
            cache_key=cache_key,
            negative=bool(data.negative),
            result_kind=result_kind,
            result_ref=(data.result_ref or "").strip(),
            result_digest=digest,
            result_payload=stored_payload,
            retention_mode=retention.mode,
            retention_reason=retention.reason,
            rights_basis=basis,
            fetched_at=data.fetched_at,
            fresh_until=fresh_until,
            schema_version=1,
            created_at=moment,
        )
        self._session.add(entry)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — cache key, source key+version, operation, negative flag, retention
        # mode+reason and the SAFE digest. The raw payload and the locator are NEVER audited.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_PUT_CACHE,
            reason=f"cached {retention.mode} source result for '{source_key}'",
            references={
                "cacheKey": cache_key,
                "sourceKey": source_key,
                "sourceVersion": data.source_version,
                "operation": operation,
                "negative": bool(data.negative),
                "retentionMode": retention.mode,
                "retentionReason": retention.reason,
                "resultDigest": digest,
                "freshUntil": fresh_until.isoformat(),
            },
        )
        return entry

    # -- internals ----------------------------------------------------------------------------------

    def _freshness_seconds(
        self, terms: SourceTerms, *, negative: bool, requested: int | None
    ) -> int:
        """The bounded freshness window (seconds) for one entry.

        A NEGATIVE entry is bounded by the configured negative TTL (and never beyond the terms window
        when the terms set one). A POSITIVE entry defaults to the terms' retention/freshness window when
        set, else the configured default, and is always clamped to the configured hard maximum (and to the
        terms window when that is tighter). A caller-requested window can only narrow, never widen.
        """
        retention = terms.retention or {}
        raw_days = retention.get("max_days")
        try:
            terms_days = None if raw_days is None else int(raw_days)
        except (TypeError, ValueError):
            terms_days = None
        terms_window = terms_days * 86400 if terms_days is not None and terms_days > 0 else None

        if negative:
            cap = self._settings.source_result_cache_negative_ttl_seconds
            if terms_window is not None:
                cap = min(cap, terms_window)
            desired = requested if requested and requested > 0 else cap
            return max(1, min(desired, cap))

        hard_max = self._settings.source_result_cache_max_fresh_seconds
        cap = min(hard_max, terms_window) if terms_window is not None else hard_max
        if requested and requested > 0:
            desired = requested
        elif terms_window is not None:
            desired = terms_window
        else:
            desired = self._settings.source_result_cache_default_fresh_seconds
        return max(1, min(desired, cap))

    def _source_exists(self, source_key: str) -> bool:
        stmt = select(Source.id).where(Source.key == source_key).limit(1)
        return self._session.scalars(stmt).first() is not None

    def _source_terms(self, source_key: str, version: int) -> SourceTerms | None:
        stmt = (
            select(SourceTerms)
            .where(SourceTerms.source_key == source_key)
            .where(SourceTerms.version == version)
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()


__all__ = [
    "AUDIT_ACTION_PUT_CACHE",
    "CacheLookup",
    "CacheResultInput",
    "SourceResultCache",
    "SourceResultCacheError",
    "REASON_MISSING_SOURCE_KEY",
    "REASON_MISSING_SOURCE_VERSION",
    "REASON_UNKNOWN_OPERATION",
    "REASON_MISSING_PURPOSE",
    "REASON_MISSING_CACHE_KEY",
    "REASON_MISSING_FETCHED_AT",
    "REASON_SOURCE_NOT_FOUND",
    "REASON_SOURCE_VERSION_NOT_FOUND",
    "REASON_NOT_CACHED",
    "REASON_STALE",
]
