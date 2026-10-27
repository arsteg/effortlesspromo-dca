"""DCA-048 — authorize a source operation before fetching (the deterministic preflight gate).

:class:`SourcePreflight` is the single sanctioned gate a would-be acquisition consumer must clear
BEFORE it ever touches a source adapter. Given a business, an action (:class:`SourceOperation`), a
field, a purpose and a moment in time, it returns one deterministic :class:`PreflightDecision` with a
stable denial reason — composing the rights foundation already built:

- **source status** (DCA-044) — the catalog entry must exist AND be usable (an approved, ENABLED
  source). A draft/disabled source is refused (:data:`REASON_SOURCE_NOT_ENABLED`); an unknown key is
  refused (:data:`REASON_SOURCE_NOT_FOUND`). Promoting a source to ENABLED stays a DISABLED future
  capability, so in the running service this gate denies every fetch — acquisition stays disabled.
- **platform ceiling + time** (DCA-045/046) — an in-force :class:`SourceTermsApproval` (active AND
  inside its validity window at ``now``) must grant the action on the field, and its terms must
  document the purpose. Otherwise refused (:data:`REASON_SOURCE_NOT_PLATFORM_APPROVED`,
  :data:`REASON_OPERATION_DENIED_AT_PLATFORM`, :data:`REASON_PURPOSE_DENIED_AT_PLATFORM`).
- **business narrowing** (DCA-047) — the tenant's in-force entitlement must be enabled and grant the
  action on the field and the purpose. Otherwise refused (:data:`REASON_SOURCE_NOT_ENTITLED`,
  :data:`REASON_OPERATION_NOT_ENTITLED`, :data:`REASON_PURPOSE_NOT_ENTITLED`).

The load-bearing guarantee (the DCA-048 acceptance criterion):

- **A denied preflight makes ZERO adapter calls, and official origin or robots permission alone can
  never bypass rights.** The ONLY sanctioned path to a source adapter is :meth:`SourcePreflight.fetch`,
  which authorizes FIRST and invokes ``adapter.fetch`` only when the decision is ``allowed``. The
  request's :attr:`PreflightRequest.origin_signals` (e.g. an "official origin" trust tier, or a
  ``robots.txt`` that permits crawling) are recorded on the decision for audit but are NEVER consulted
  by :meth:`authorize` — so an official registry or a permissive ``robots.txt`` can never, on its own,
  authorize a fetch the rights chain withholds.

:meth:`authorize` is a pure, side-effect-free read: it stages nothing, commits nothing and calls no
adapter, so a denial (or an allow) is perfectly reproducible with no state change. The gate is bound
to one verified business scope (like the DCA-047 entitlement store) so a tenant's preflight can only
consult that tenant's entitlements.

This story adds no acquisition: no new source is enabled, no live adapter ships, and the catalog's
ENABLED state stays a disabled future capability. The gate simply makes the authorize-before-fetch
decision explicit, deterministic and auditable for a later (still disabled) acquisition consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.db.models import SourceOperation
from app.services.source_catalog import (
    REASON_SOURCE_NOT_ENABLED,
    SourceCatalogStore,
)
from app.services.source_entitlement import (
    REASON_OPERATION_DENIED_AT_PLATFORM,
    REASON_OPERATION_NOT_ENTITLED,
    REASON_PURPOSE_DENIED_AT_PLATFORM,
    REASON_SOURCE_NOT_ENTITLED,
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    BusinessSourceEntitlementStore,
)

# Request-shape refusals (raised deterministically before any store is consulted).
REASON_MISSING_SOURCE = "missing_source_key"
REASON_UNKNOWN_OPERATION = "unknown_operation"
REASON_MISSING_PURPOSE = "missing_purpose"

# The source-status refusal unique to the preflight: the catalog has no such source at all.
REASON_SOURCE_NOT_FOUND = "source_not_found"

# Purpose narrowed-read denial (the business entitlement does not cover the requested purpose). The
# platform-level purpose denial reuses DCA-047's REASON_PURPOSE_DENIED_AT_PLATFORM.
REASON_PURPOSE_NOT_ENTITLED = "purpose_not_entitled"

# The catalog-status refusals reused from DCA-044 (a draft/disabled source is not usable).
# The ceiling / entitlement refusals reused from DCA-047 (platform + business rights).
__all_reasons__ = (
    REASON_MISSING_SOURCE,
    REASON_UNKNOWN_OPERATION,
    REASON_MISSING_PURPOSE,
    REASON_SOURCE_NOT_FOUND,
    REASON_SOURCE_NOT_ENABLED,
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    REASON_OPERATION_DENIED_AT_PLATFORM,
    REASON_PURPOSE_DENIED_AT_PLATFORM,
    REASON_SOURCE_NOT_ENTITLED,
    REASON_OPERATION_NOT_ENTITLED,
    REASON_PURPOSE_NOT_ENTITLED,
)


@dataclass(frozen=True)
class PreflightRequest:
    """One source-operation authorization request, evaluated before any fetch.

    ``source_key`` is the catalog source; ``operation`` the action (a :class:`SourceOperation` value,
    e.g. ``collect``); ``field`` the specific field (empty means the source-wide grant); ``purpose``
    the purpose the data would be used for (required — rights are purpose-bound, never blanket).

    ``origin_signals`` are advisory provenance hints a caller may attach (e.g.
    ``{"official_origin": True, "robots_allowed": True}``). They are recorded on the decision for
    audit but are NEVER consulted by :meth:`SourcePreflight.authorize`: an official origin or a
    permissive ``robots.txt`` can never, on its own, authorize a fetch the rights chain withholds.
    """

    source_key: str
    operation: str
    purpose: str
    field: str = ""
    origin_signals: dict = dc_field(default_factory=dict)


@dataclass(frozen=True)
class PreflightDecision:
    """The deterministic outcome of a preflight. ``allowed`` with no ``reason`` on success; otherwise
    ``allowed=False`` with a STABLE ``reason`` (and ``detail`` naming the offending value).

    ``evidence`` records the dimensions the decision was made on (business, action, field, purpose,
    the ``now`` it was evaluated at, the resolved source status, and the advisory ``origin_signals``
    that were IGNORED) so a reader can reproduce and audit exactly why a fetch was allowed or denied.
    """

    allowed: bool
    reason: str | None = None
    detail: str | None = None
    evidence: dict = dc_field(default_factory=dict)


@dataclass(frozen=True)
class PreflightFetchResult:
    """The result of a guarded :meth:`SourcePreflight.fetch`.

    ``adapter_called`` is the load-bearing witness: it is ``True`` only when the preflight allowed the
    operation and the adapter was actually invoked. On any denial it is ``False`` and ``candidates``
    is ``None`` — the adapter was never touched.
    """

    decision: PreflightDecision
    adapter_called: bool
    candidates: list | None = None


class _Fetchable(Protocol):
    """The minimal shape of a source adapter the preflight guards (see :mod:`app.sources.base`)."""

    key: str

    def fetch(self, icp: dict) -> list: ...


class SourcePreflight:
    """Deterministic authorize-before-fetch gate for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it
    from the membership-verified tenant context). Composes the DCA-044 catalog, the DCA-045/046
    platform approval ceiling and the DCA-047 business entitlement into a single decision.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._catalog = SourceCatalogStore(session)
        self._entitlements = BusinessSourceEntitlementStore(session, business_id)

    # -- the one decision (pure, side-effect-free: no adapter, no state change) ---------------------

    def authorize(
        self, request: PreflightRequest, *, now: datetime | None = None
    ) -> PreflightDecision:
        """Decide whether ``request`` may proceed to a fetch. A pure read — stages nothing, commits
        nothing, calls no adapter — so the result is perfectly reproducible.

        The dimensions are evaluated deny-by-default in a FIXED order, stopping at the first failure:
        request shape → source status (catalog) → platform ceiling + purpose (at ``now``) → business
        entitlement + purpose. ``request.origin_signals`` are recorded in the evidence but never
        affect the decision.
        """
        source_key = (request.source_key or "").strip()
        operation_raw = (request.operation or "").strip().lower()
        field_name = (request.field or "").strip()
        purpose = (request.purpose or "").strip()
        evidence: dict = {
            "businessId": self._business_id,
            "operation": operation_raw,
            "field": field_name,
            "purpose": purpose,
            "evaluatedAt": now.isoformat() if now is not None else None,
            # Recorded for audit, DELIBERATELY not consulted by the decision below.
            "ignoredOriginSignals": dict(request.origin_signals or {}),
        }

        # 1) Request shape — a source key, a recognized operation and a purpose are all required.
        if not source_key:
            return self._deny(REASON_MISSING_SOURCE, evidence, detail=None)
        evidence["sourceKey"] = source_key
        try:
            operation = SourceOperation(operation_raw).value
        except ValueError:
            return self._deny(REASON_UNKNOWN_OPERATION, evidence, detail=request.operation)
        if not purpose:
            return self._deny(REASON_MISSING_PURPOSE, evidence, detail=None)

        # 2) Source status (DCA-044) — the source must be catalogued AND usable (approved + ENABLED).
        source = self._catalog.get(source_key)
        if source is None:
            return self._deny(REASON_SOURCE_NOT_FOUND, evidence, detail=source_key)
        evidence["sourceStatus"] = source.status
        evidence["sourceEnabled"] = source.enabled
        # An official-registry trust tier is recorded, never a bypass: the rights chain still decides.
        evidence["sourceTrustTier"] = source.trust_tier
        if not SourceCatalogStore.is_usable(source):
            return self._deny(REASON_SOURCE_NOT_ENABLED, evidence, detail=source_key)

        # 3) Platform ceiling + business narrowing on the (field, operation) — reuse the DCA-047
        #    intersection read (in-force approval at ``now`` AND the tenant's enabled entitlement).
        allowed, reason = self._entitlements.permits(source_key, field_name, operation, now=now)
        if not allowed:
            return self._deny(reason or REASON_OPERATION_NOT_ENTITLED, evidence, detail=field_name)

        # 4) Purpose — the request purpose must be documented by the platform approval AND covered by
        #    the business entitlement (rights are purpose-bound; a purpose outside either is refused).
        approval = self._entitlements.in_force_approval(source_key, now=now)
        # ``permits`` already proved an in-force approval exists; narrow belt-and-braces guard anyway.
        if approval is None:
            return self._deny(REASON_SOURCE_NOT_PLATFORM_APPROVED, evidence, detail=source_key)
        if purpose not in self._entitlements.approved_purposes(approval):
            return self._deny(REASON_PURPOSE_DENIED_AT_PLATFORM, evidence, detail=purpose)
        entitlement = self._entitlements.latest(source_key)
        entitled_purposes = {str(p) for p in (entitlement.purposes if entitlement else []) or []}
        if purpose not in entitled_purposes:
            return self._deny(REASON_PURPOSE_NOT_ENTITLED, evidence, detail=purpose)

        return PreflightDecision(allowed=True, reason=None, evidence=evidence)

    @staticmethod
    def _deny(reason: str, evidence: dict, *, detail: str | None) -> PreflightDecision:
        return PreflightDecision(allowed=False, reason=reason, detail=detail, evidence=evidence)

    # -- the one guarded path to an adapter ---------------------------------------------------------

    def fetch(
        self,
        adapter: _Fetchable,
        request: PreflightRequest,
        icp: dict,
        *,
        now: datetime | None = None,
    ) -> PreflightFetchResult:
        """Authorize ``request`` and, ONLY when allowed, invoke ``adapter.fetch(icp)``.

        This is the single sanctioned path from a would-be acquisition consumer to a source adapter:
        a denied preflight returns immediately with ``adapter_called=False`` and ``candidates=None``,
        so a denial makes ZERO adapter calls. Because :meth:`authorize` ignores ``origin_signals``,
        an official origin or a permissive ``robots.txt`` can never reach the adapter on its own.
        """
        decision = self.authorize(request, now=now)
        if not decision.allowed:
            return PreflightFetchResult(decision=decision, adapter_called=False, candidates=None)
        candidates = list(adapter.fetch(icp))
        return PreflightFetchResult(decision=decision, adapter_called=True, candidates=candidates)
