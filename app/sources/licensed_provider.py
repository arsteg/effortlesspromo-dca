"""DCA-125 — enrich an organization from ONE licensed provider (the third concrete source adapter).

Key: ``licensed-adapter`` · source sections 6, 8, 9. Stage: Approved source access.

The first two concrete adapters cost nothing: DCA-123 imported an approved dataset and DCA-124 parsed an
approved page deterministically. This is the FIRST adapter whose data comes from a PAID/LICENSED B2B
provider — every call is metered and chargeable — so the load-bearing addition is that the call flows
through the cost machinery: it RESERVES budget before the call (DCA-113), records the call LIFECYCLE
(DCA-115) and RECONCILES the hold against the actual cost (DCA-114). It builds on, and never bypasses,
the already-built chain:

- the DCA-122 source-adapter capability contract — the adapter carries an ORGANIZATION-ONLY capability
  descriptor (:data:`LICENSED_PROVIDER_CAPABILITY`), declares no personal-contact operation, and gates
  its one operation through the descriptor, so it can never declare or record a contact field;
- the DCA-048 rights preflight (:class:`~app.services.source_preflight.SourcePreflight`) — checked
  FIRST: the fetch proceeds only when the source is approved AND the business is entitled to ``collect``
  for the purpose; a rights denial reserves no budget, makes NO provider call and writes nothing;
- the DCA-113/114/115 cost ledger — budget is reserved up front (an over-budget reservation is refused
  and makes NO provider call), the provider call's lifecycle is recorded, and the hold is reconciled
  against the reported actual cost; spend is therefore bounded (never past the caps) and tracked;
- the DCA-059/061 observation stores — the returned organization fields are recorded as provenanced
  :class:`~app.db.models.FieldObservation` rows for an organization observation the business already
  holds; a returned personal/contact field is DROPPED, never recorded.

**No real/paid provider call is ever made.** The provider is reached only through the
:class:`~app.sources.licensed_provider_contract.LicensedProviderClient` port; the ONLY implementation is
the synthetic fake (:mod:`app.sources.licensed_provider_fake`), which performs no network call and is
refused in production — the live licensed-provider egress stays a DISABLED future capability.

The load-bearing guarantees (the DCA-125 acceptance criteria):

- **Gated on approved + entitled rights.** A source that is not approved+entitled for the business and
  purpose is refused at the rights phase (DCA-048); the fetch reserves nothing, calls nothing, records
  nothing and changes no state.
- **The paid call is bounded and tracked.** Budget is reserved BEFORE the call (an over-budget request
  is refused with no call and no spend); the provider call is recorded PENDING → IN_FLIGHT → terminal;
  and the hold is reconciled — SETTLED at the reported actual cost on success (unused headroom released),
  RELEASED in full on a confirmed failure, or PRESERVED (counts in full, awaiting the DCA-116 resolver)
  on an UNCERTAIN outcome. Spend can never exceed the caps the reserve path admitted it under.
- **Returned data is recorded as provenanced observations.** Each permitted organization field becomes
  an observed :class:`~app.db.models.FieldObservation` citing the provider-call provenance; an
  organization-only guard DROPS any personal/contact field (counted, never recorded).
- **A fake/contract in dev; denials do nothing.** The provider is a synthetic fake (no live/paid call);
  an unentitled or over-budget fetch writes nothing; and a re-fetch under the same fetch key is a
  deterministic no-op (no second reservation, no second call).

The adapter stages into the caller's session and never commits — the caller owns the unit of work, so
the field observations, the reservation/provider-call/reconciliation ledger rows, their atomic audit and
the run-level provenance row all commit together.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor
from app.cost.provider_call import (
    ProviderCallActor,
    ProviderCallIntent,
    ProviderCallStore,
    ProviderCallTransition,
)
from app.cost.reconcile import (
    BudgetReconciliationStore,
    ReconciliationActor,
    ReconciliationRequest,
    ReconcileOutcome,
)
from app.cost.reserve import (
    BudgetReservationStore,
    ReservationActor,
    ReservationError,
    ReservationRequest,
)
from app.db.models import (
    FieldInferenceLabel,
    FieldSubjectType,
    LicensedProviderFetch,
    ProviderCallStatus,
    SourceAccessMethod,
    SourceKind,
    SourceOperation,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import OrgObservationStore
from app.services.source_preflight import PreflightRequest, SourcePreflight
from app.sources.capabilities import (
    AdapterOperation,
    SourceAdapterCapability,
    validate_capability,
)
from app.sources.licensed_provider_contract import (
    LicensedProviderClient,
    LicensedProviderOutcome,
    LicensedProviderRequest,
    LicensedProviderResult,
)

# The adapter key (distinct from a catalog Source key — the provider's source is named in the request).
ADAPTER_KEY = "dca.licensed_provider"

# The ORGANIZATION-ONLY capability this adapter declares (DCA-122). It enriches organization-level fields
# for a KNOWN organization; it touches NO personal contact data, so ``handles_personal_contacts`` is
# False and it declares no ``ENRICH_CONTACT`` operation — the structural guarantee that this adapter can
# never read or write a contact field. Validated at import time so an incoherent descriptor fails loudly.
LICENSED_PROVIDER_CAPABILITY: SourceAdapterCapability = validate_capability(
    SourceAdapterCapability(
        key=ADAPTER_KEY,
        source_kind=SourceKind.COMMERCIAL,
        access_method=SourceAccessMethod.API,
        operations=frozenset(
            {
                AdapterOperation.LIST_CAPABILITIES,
                AdapterOperation.ENRICH_ORGANIZATION,
            }
        ),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=False,
        trust_tier="licensed_provider",
        notes="DCA-125 licensed B2B provider adapter (organization-only; paid call DISABLED — "
        "synthetic fake only)",
    )
)

# The source RIGHT (DCA-045 per-field operation) a licensed fetch depends on, the AI/capability label the
# provider-call lifecycle records, and the version of the fetch contract provenance rows are written
# under (expand→migrate→contract).
FETCH_OPERATION = SourceOperation.COLLECT.value
FETCH_CAPABILITY = "contact_research"
FETCH_SCHEMA_VERSION = 1

# The ALLOWLIST of ORGANIZATION-level fields this adapter may record. Every key is a company fact; the
# list contains NO contact channel, so the adapter has no permitted field that could carry personal
# contact data. A returned field whose key is not on this list is DROPPED, regardless of its value.
PERMITTED_ORG_FIELDS: frozenset[str] = frozenset(
    {"legal_name", "industry", "employee_count", "annual_revenue_band", "hq_state", "website"}
)

# Personal/contact fragments the adapter explicitly DETECTS in a returned VALUE and DROPS (counted, never
# recorded) — a belt-and-braces screen so even an allowlisted key carrying a contact value cannot leak.
_CONTACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"""),  # email address
    re.compile(r"""(?<!\d)(?:\+91[\s\-]?)?[6-9]\d{9}(?!\d)"""),  # Indian mobile number
)

# Request-shape / schema refusals — each raised BEFORE any state is read, so a refusal writes nothing.
REASON_MISSING_SOURCE_KEY = "request_missing_source_key"
REASON_MISSING_SOURCE_VERSION = "request_missing_source_version"
REASON_MISSING_PURPOSE = "request_missing_purpose"
REASON_MISSING_ORG_REF = "request_missing_org_ref"
REASON_MISSING_FETCH_KEY = "request_missing_fetch_key"
REASON_INVALID_ESTIMATE = "request_invalid_cost_estimate"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"
REASON_NOT_AUTHORIZED = "licensed_fetch_not_authorized"


class LicensedProviderError(Exception):
    """A licensed-fetch request/shape problem refused the fetch with a stable code. Raised before the
    session is mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending
    value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class LicensedProviderAuthorizationError(LicensedProviderError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`.

    Fetching from a PAID provider reserves and reconciles spend, so it is a budget-control action — an
    ordinary acting user and a read-only viewer lack it, and a platform operator holds no business scope
    for the tenant. Raised FIRST (before any state is read or written), so an unauthorized attempt leaves
    no trace and makes no provider call. ``code`` is always :data:`REASON_NOT_AUTHORIZED`."""

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class LicensedProviderActor:
    """The business principal running a licensed-provider fetch.

    ``subject`` is an opaque actor id (never a name/email) recorded on every row and its atomic audit
    entry. ``roles`` are the caller's verified business-plane membership roles (never a bare token claim).
    :meth:`may_fetch` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` — a paid fetch reserves and reconciles spend, so
    it is an administrator action."""

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_fetch(self) -> bool:
        return Scope.BUDGET_MANAGE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))

    def as_reservation_actor(self) -> ReservationActor:
        return ReservationActor(subject=self.subject, roles=self.roles, actor_type=self.actor_type)

    def as_provider_call_actor(self) -> ProviderCallActor:
        return ProviderCallActor(subject=self.subject, roles=self.roles, actor_type=self.actor_type)

    def as_reconciliation_actor(self) -> ReconciliationActor:
        return ReconciliationActor(
            subject=self.subject, roles=self.roles, actor_type=self.actor_type
        )


@dataclass(frozen=True)
class LicensedProviderFetchRequest:
    """One would-be licensed-provider organization fetch for an observation the business already holds.

    ``subject_observation_id`` is the :class:`~app.db.models.OrgObservation` (in THIS business) the
    fetched fields are about. ``source_key`` / ``source_version`` name the approved+entitled catalog
    source and its terms version the provider is read under; ``purpose`` the purpose the fetch serves
    (rights are purpose-bound). ``org_ref`` is the opaque organization reference handed to the provider
    (never a personal identifier). ``fetch_key`` is the idempotency token for this logical fetch (a retry
    with the same key is a replay, never a second reservation or call). ``estimated_cost_minor`` is the
    CONSERVATIVE up-front estimate (INR minor units) reserved before the call — a positive integer.
    ``provider`` names the cost bucket the call is attributed to. ``scope`` is the India-only collection
    scope recorded on each fact; ``rights_basis`` the recorded rights basis (derived from the purpose when
    blank)."""

    subject_observation_id: str
    source_key: str
    source_version: int
    purpose: str
    org_ref: str
    fetch_key: str
    estimated_cost_minor: int
    provider: str = ""
    scope: str = "nationwide"
    rights_basis: str = ""


@dataclass(frozen=True)
class LicensedProviderFetchResult:
    """The deterministic outcome of one licensed fetch (no real/paid call; inert on a denial).

    ``authorized`` is the gate verdict; on a denial ``phase`` is which phase decided it
    (``rights``/``budget``), ``reason`` the stable denial code, and everything else empty (nothing
    reserved, called or written). On success ``skipped`` marks the idempotent no-op. On a fresh run
    ``outcome`` is the provider-reported outcome, ``call_status`` the provider-call lifecycle status,
    ``reservation_id`` / ``provider_call_id`` the ledger rows, ``reserved_minor`` the hold taken,
    ``settled_minor`` what it reconciled to (``None`` when preserved as UNCERTAIN), ``released_minor`` the
    freed headroom, ``spent_minor`` the effective bounded spend, ``recorded`` the permitted organization
    facts written (with ``field_observation_ids``), ``contact_fields_dropped`` the dropped personal
    fragments, and ``needs_review`` whether the hold is preserved awaiting the DCA-116 resolver."""

    authorized: bool
    subject_id: str
    source_key: str
    source_version: int
    provider: str
    fetch_key: str
    phase: str = ""
    reason: str | None = None
    skipped: bool = False
    outcome: str = ""
    call_status: str = ""
    reservation_id: str | None = None
    provider_call_id: str | None = None
    reserved_minor: int = 0
    settled_minor: int | None = None
    released_minor: int = 0
    spent_minor: int = 0
    recorded: tuple[tuple[str, str], ...] = ()
    field_observation_ids: tuple[str, ...] = ()
    contact_fields_dropped: int = 0
    needs_review: bool = False

    @property
    def fields_recorded(self) -> int:
        return len(self.field_observation_ids)


def _is_contact_value(value: str) -> bool:
    """True if ``value`` looks like a personal/contact fragment (email / Indian mobile) — such a value is
    DROPPED, never recorded, even if its field key were allowlisted."""
    return any(p.search(value) for p in _CONTACT_PATTERNS)


class LicensedProviderAdapter:
    """Fetch organization fields from ONE licensed provider, for ONE business (tenant-scoped).

    Construct with the active session, the verified business scope (a route/worker resolves it from the
    membership-verified tenant context) and the licensed-provider client (the synthetic fake today; the
    live adapter is DISABLED). Composes the DCA-048 rights preflight (checked FIRST), the DCA-113/114/115
    cost ledger (reserve → call → reconcile) and the DCA-059/061 observation stores; it records only
    organization facts and their provenance, never a contact, and never performs a real/paid call.
    """

    capability: SourceAdapterCapability = LICENSED_PROVIDER_CAPABILITY

    def __init__(
        self, session: Session, business_id: str, provider_client: LicensedProviderClient
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._provider_client = provider_client
        self._preflight = SourcePreflight(session, business_id)
        self._reservations = BudgetReservationStore(session, business_id)
        self._calls = ProviderCallStore(session, business_id)
        self._reconciler = BudgetReconciliationStore(session, business_id)
        self._observations = OrgObservationStore(session, business_id)
        self._fields = FieldObservationStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def fetch_for(
        self, source_key: str, subject_id: str, fetch_key: str
    ) -> LicensedProviderFetch | None:
        """The fetch-provenance row for one logical fetch + subject in this business, or ``None``."""
        stmt = (
            select(LicensedProviderFetch)
            .where(LicensedProviderFetch.business_id == self._business_id)
            .where(LicensedProviderFetch.source_key == (source_key or "").strip())
            .where(LicensedProviderFetch.subject_id == (subject_id or "").strip())
            .where(LicensedProviderFetch.fetch_key == (fetch_key or "").strip())
        )
        return self._session.scalars(stmt).one_or_none()

    def fetches_for(self, source_key: str, subject_id: str) -> list[LicensedProviderFetch]:
        """Every fetch-provenance row recorded for one subject + source in this business (oldest first)."""
        stmt = (
            select(LicensedProviderFetch)
            .where(LicensedProviderFetch.business_id == self._business_id)
            .where(LicensedProviderFetch.source_key == (source_key or "").strip())
            .where(LicensedProviderFetch.subject_id == (subject_id or "").strip())
            .order_by(LicensedProviderFetch.created_at, LicensedProviderFetch.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one fetch path -------------------------------------------------------------------------

    def fetch(
        self,
        request: LicensedProviderFetchRequest,
        actor: LicensedProviderActor,
        *,
        now: datetime | None = None,
    ) -> LicensedProviderFetchResult:
        """Authorize, reserve, call the (synthetic) provider, reconcile, and record the organization facts.

        Order (each stage writes nothing on failure): descriptor gate + request shape (raises
        :class:`LicensedProviderError`) → authority (raises :class:`LicensedProviderAuthorizationError`) →
        subject must exist in THIS business (raises) → idempotency skip (an already-run fetch is a no-op)
        → the DCA-048 rights preflight (a denial reserves/calls/writes nothing) → DCA-113 budget reserve
        (an over-budget request is refused with no call and no spend) → DCA-115 provider-call lifecycle
        (PENDING → IN_FLIGHT) → the provider call via the port (a SYNTHETIC fake — no real/paid call) →
        terminal transition + DCA-114 reconcile (settle / release / preserve) → record each permitted
        organization fact as a provenanced observation (contacts dropped) → stage the run provenance row.
        """
        moment = now or datetime.now(UTC)

        # Descriptor gate (DCA-122): the adapter does only what it declares.
        self.capability.assert_supports(AdapterOperation.ENRICH_ORGANIZATION)

        # 1) Request shape — a source + version, a purpose, an org ref, a fetch key and a positive
        #    up-front estimate are all required, raised before any state is read.
        source_key = (request.source_key or "").strip()
        if not source_key:
            raise LicensedProviderError(REASON_MISSING_SOURCE_KEY, "a source_key is required")
        if request.source_version is None:
            raise LicensedProviderError(
                REASON_MISSING_SOURCE_VERSION, "a source_version is required"
            )
        purpose = (request.purpose or "").strip()
        if not purpose:
            raise LicensedProviderError(REASON_MISSING_PURPOSE, "a purpose is required")
        org_ref = (request.org_ref or "").strip()
        if not org_ref:
            raise LicensedProviderError(
                REASON_MISSING_ORG_REF, "an org_ref (the provider lookup reference) is required"
            )
        fetch_key = (request.fetch_key or "").strip()
        if not fetch_key:
            raise LicensedProviderError(
                REASON_MISSING_FETCH_KEY, "a fetch_key (the idempotency token) is required"
            )
        estimate = int(request.estimated_cost_minor)
        if estimate <= 0:
            raise LicensedProviderError(
                REASON_INVALID_ESTIMATE,
                f"estimated_cost_minor must be a positive number of INR minor units (got {estimate})",
                detail=str(estimate),
            )
        provider = (request.provider or "").strip() or self._provider_client_name()

        # 2) Authority FIRST — a paid fetch reserves/reconciles spend, so it needs BUDGET_MANAGE.
        if not actor.may_fetch():
            raise LicensedProviderAuthorizationError(
                "a licensed-provider fetch reserves and reconciles PAID spend and requires a business "
                f"administrator holding '{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a "
                "read-only viewer or a platform operator (holding no business scope) cannot perform it"
            )

        # 3) Subject — the facts must attach to an organization observation IN THIS BUSINESS. An unknown
        #    id, or one owned by another business, reads as not-found (never a cross-tenant link).
        subject_id = (request.subject_observation_id or "").strip()
        if not subject_id or self._observations.get(subject_id) is None:
            raise LicensedProviderError(
                REASON_SUBJECT_NOT_FOUND,
                f"organization observation '{subject_id}' does not exist in this business",
                detail=subject_id,
            )

        # 4) Idempotency — a logical fetch already run for this subject is a deterministic no-op. Checked
        #    BEFORE authorizing/reserving, so a replay reserves no budget and makes no provider call.
        existing = self.fetch_for(source_key, subject_id, fetch_key)
        if existing is not None:
            return self._skipped_result(existing)

        # 5) Rights FIRST (DCA-048) — a denial reserves nothing, calls nothing and writes nothing.
        decision = self._preflight.authorize(
            PreflightRequest(
                source_key=source_key, operation=FETCH_OPERATION, purpose=purpose, field=""
            ),
            now=now,
        )
        if not decision.allowed:
            return LicensedProviderFetchResult(
                authorized=False,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                provider=provider,
                fetch_key=fetch_key,
                phase="rights",
                reason=decision.reason,
            )

        # 6) Budget reserve (DCA-113) — BEFORE the paid call. An over-budget request is refused here, so
        #    no provider call is made and nothing is spent or written.
        reserve_key = f"licensed:{provider}:{fetch_key}:reserve"
        call_key = f"licensed:{provider}:{fetch_key}:call"
        try:
            reservation = self._reservations.reserve(
                ReservationRequest(
                    idempotency_key=reserve_key, amount_minor=estimate, provider=provider
                ),
                actor.as_reservation_actor(),
                now=now,
            )
        except ReservationError as exc:
            # Over-budget / cap-exceeded: the paid call never happens and the ledger is unchanged.
            return LicensedProviderFetchResult(
                authorized=False,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                provider=provider,
                fetch_key=fetch_key,
                phase="budget",
                reason=exc.code,
            )
        self._session.flush()  # materialize reservation.id for the provider-call link + provenance
        reserved_minor = int(reservation.amount_minor)

        # 7) Provider-call lifecycle (DCA-115) — record the intent PENDING, then dispatch (IN_FLIGHT).
        request_id = f"dca:licensed:{fetch_key}"
        call_actor = actor.as_provider_call_actor()
        self._calls.record(
            ProviderCallIntent(
                call_key=call_key,
                capability=FETCH_CAPABILITY,
                request_id=request_id,
                intent="licensed_org_enrichment",
                provider=provider,
                model="",
                source_version=str(request.source_version),
                reservation_id=reservation.id,
            ),
            call_actor,
            now=now,
        )
        self._session.flush()  # materialize the provider-call id
        record = self._calls.transition(
            ProviderCallTransition(call_key, ProviderCallStatus.IN_FLIGHT), call_actor, now=now
        )
        provider_call_id = record.id

        # 8) THE PROVIDER CALL — via the port only. The ONLY client is a SYNTHETIC fake (no network, no
        #    real/paid call); the live licensed-provider egress stays a disabled future capability.
        provider_result = self._provider_client.fetch_organization(
            LicensedProviderRequest(
                business_id=self._business_id,
                org_ref=org_ref,
                request_id=request_id,
                requested_fields=tuple(sorted(PERMITTED_ORG_FIELDS)),
            )
        )

        # 9) Terminal transition + reconcile the hold against what actually happened.
        provenance_ref = f"licensed:{provider}:call={provider_call_id}"
        recorded: list[tuple[str, str]] = []
        field_observation_ids: list[str] = []
        contact_dropped = 0
        needs_review = False

        if provider_result.outcome is LicensedProviderOutcome.SUCCEEDED:
            self._calls.transition(
                ProviderCallTransition(
                    call_key,
                    ProviderCallStatus.SUCCEEDED,
                    outcome_code=provider_result.outcome_code or "completed",
                    prompt_tokens=provider_result.prompt_tokens,
                    completion_tokens=provider_result.completion_tokens,
                    total_tokens=provider_result.total_tokens,
                ),
                call_actor,
                now=now,
            )
            recon = self._reconciler.reconcile(
                ReconciliationRequest(
                    reservation_id=reservation.id,
                    outcome_key=call_key,
                    outcome=ReconcileOutcome.SETTLE,
                    actual_minor=int(provider_result.cost_minor),
                ),
                actor.as_reconciliation_actor(),
                now=now,
            )
            # Record each PERMITTED organization fact; DROP any contact field the provider returned.
            recorded, field_observation_ids, contact_dropped = self._record_fields(
                subject_id=subject_id,
                result=provider_result,
                source_key=source_key,
                source_version=request.source_version,
                scope=request.scope,
                rights_basis=request.rights_basis,
                purpose=purpose,
                provenance_ref=provenance_ref,
                actor=actor.as_audit_actor(),
                moment=moment,
            )
        elif provider_result.outcome is LicensedProviderOutcome.FAILED:
            self._calls.transition(
                ProviderCallTransition(
                    call_key,
                    ProviderCallStatus.FAILED,
                    outcome_code=provider_result.outcome_code or "provider_rejected",
                ),
                call_actor,
                now=now,
            )
            recon = self._reconciler.reconcile(
                ReconciliationRequest(
                    reservation_id=reservation.id,
                    outcome_key=call_key,
                    outcome=ReconcileOutcome.RELEASE,
                ),
                actor.as_reconciliation_actor(),
                now=now,
            )
        else:  # UNCERTAIN — dispatched then unconfirmed: preserve the hold, trust/record NO data.
            self._calls.transition(
                ProviderCallTransition(
                    call_key,
                    ProviderCallStatus.UNCERTAIN,
                    outcome_code=provider_result.outcome_code or "gateway_timeout",
                ),
                call_actor,
                now=now,
            )
            recon = self._reconciler.reconcile(
                ReconciliationRequest(
                    reservation_id=reservation.id,
                    outcome_key=call_key,
                    outcome=ReconcileOutcome.MARK_UNCERTAIN,
                ),
                actor.as_reconciliation_actor(),
                now=now,
            )
            needs_review = True

        # Effective bounded spend: the settled amount on success, 0 on a confirmed failure, and the full
        # preserved hold while the outcome is UNCERTAIN (the hold still counts against the caps).
        if provider_result.outcome is LicensedProviderOutcome.UNCERTAIN:
            spent_minor = reserved_minor
            settled_minor: int | None = None
        else:
            settled_minor = recon.settled_minor
            spent_minor = recon.settled_minor or 0

        # 10) Stage the run-level fetch provenance (the idempotency key + the cost/outcome attribution).
        self._session.add(
            LicensedProviderFetch(
                id=str(uuid.uuid4()),
                business_id=self._business_id,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                provider=provider,
                fetch_key=fetch_key,
                reservation_id=reservation.id,
                provider_call_id=provider_call_id,
                outcome=provider_result.outcome.value,
                call_status=record.status
                if provider_result.outcome is LicensedProviderOutcome.FAILED
                else provider_result.outcome.value,
                fields_recorded=len(field_observation_ids),
                contact_fields_dropped=contact_dropped,
                reserved_minor=reserved_minor,
                settled_minor=settled_minor,
                needs_review=needs_review,
                schema_version=FETCH_SCHEMA_VERSION,
                created_at=moment,
            )
        )

        return LicensedProviderFetchResult(
            authorized=True,
            subject_id=subject_id,
            source_key=source_key,
            source_version=request.source_version,
            provider=provider,
            fetch_key=fetch_key,
            phase="call",
            outcome=provider_result.outcome.value,
            call_status=provider_result.outcome.value,
            reservation_id=reservation.id,
            provider_call_id=provider_call_id,
            reserved_minor=reserved_minor,
            settled_minor=settled_minor,
            released_minor=recon.released_minor,
            spent_minor=spent_minor,
            recorded=tuple(recorded),
            field_observation_ids=tuple(field_observation_ids),
            contact_fields_dropped=contact_dropped,
            needs_review=needs_review,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _provider_client_name(self) -> str:
        """The default cost bucket — the provider the client speaks for (advisory; opaque)."""
        return getattr(self._provider_client, "_provider", "licensed_provider")

    def _record_fields(
        self,
        *,
        subject_id: str,
        result: LicensedProviderResult,
        source_key: str,
        source_version: int,
        scope: str,
        rights_basis: str,
        purpose: str,
        provenance_ref: str,
        actor: AuditActor,
        moment: datetime,
    ) -> tuple[list[tuple[str, str]], list[str], int]:
        """Record each PERMITTED organization field as a provenanced observation; DROP contact fields.

        A returned field is recorded ONLY when its key is on :data:`PERMITTED_ORG_FIELDS` AND its value is
        not a personal/contact fragment; anything else is DROPPED (counted, never recorded) — the
        organization-only guard (DCA-122). Returns ``(recorded, observation_ids, contact_fields_dropped)``.
        """
        basis = (rights_basis or "").strip() or f"approved_source:{source_key}/collect/{purpose}"
        recorded: list[tuple[str, str]] = []
        observation_ids: list[str] = []
        dropped = 0
        for field in result.fields:
            key = field.field_key.strip().lower()
            value = field.field_value.strip()
            if key not in PERMITTED_ORG_FIELDS or _is_contact_value(value):
                # Not an allowlisted company field (or a contact value) — DROP it, never record it.
                dropped += 1
                continue
            observation = self._fields.record(
                FieldObservationInput(
                    subject_type=FieldSubjectType.ORGANIZATION.value,
                    subject_id=subject_id,
                    field_key=key,
                    field_value=value,
                    inference_label=FieldInferenceLabel.OBSERVED.value,
                    provenance_ref=provenance_ref,
                    observed_at=moment,
                    scope=scope,
                    rights_basis=basis,
                    source_key=source_key,
                    source_version=source_version,
                ),
                actor=actor,
                now=moment,
            )
            observation_ids.append(observation.id)
            recorded.append((key, value))
        return recorded, observation_ids, dropped

    def _skipped_result(self, row: LicensedProviderFetch) -> LicensedProviderFetchResult:
        """Rebuild the inert result for an already-run fetch (the idempotent no-op)."""
        return LicensedProviderFetchResult(
            authorized=True,
            subject_id=row.subject_id,
            source_key=row.source_key,
            source_version=row.source_version,
            provider=row.provider,
            fetch_key=row.fetch_key,
            phase="call",
            skipped=True,
            outcome=row.outcome,
            call_status=row.call_status,
            reservation_id=row.reservation_id,
            provider_call_id=row.provider_call_id,
            reserved_minor=int(row.reserved_minor or 0),
            settled_minor=row.settled_minor,
            released_minor=max(0, int(row.reserved_minor or 0) - int(row.settled_minor or 0))
            if row.settled_minor is not None
            else 0,
            spent_minor=int(row.settled_minor)
            if row.settled_minor is not None
            else int(row.reserved_minor or 0)
            if row.needs_review
            else 0,
            contact_fields_dropped=int(row.contact_fields_dropped or 0),
            needs_review=bool(row.needs_review),
        )


__all__ = [
    "ADAPTER_KEY",
    "LICENSED_PROVIDER_CAPABILITY",
    "PERMITTED_ORG_FIELDS",
    "FETCH_OPERATION",
    "FETCH_CAPABILITY",
    "LicensedProviderActor",
    "LicensedProviderFetchRequest",
    "LicensedProviderFetchResult",
    "LicensedProviderAdapter",
    "LicensedProviderError",
    "LicensedProviderAuthorizationError",
    "REASON_MISSING_SOURCE_KEY",
    "REASON_MISSING_SOURCE_VERSION",
    "REASON_MISSING_PURPOSE",
    "REASON_MISSING_ORG_REF",
    "REASON_MISSING_FETCH_KEY",
    "REASON_INVALID_ESTIMATE",
    "REASON_SUBJECT_NOT_FOUND",
    "REASON_NOT_AUTHORIZED",
]
