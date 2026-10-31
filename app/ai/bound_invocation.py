"""DCA-130 — bound an AI-service (model) invocation's cost and retries.

Key: ``ai-budget`` · source sections 4, 8, 9. Stage: Existing AI-service integration.

One capability: wrap ONE model invocation through the existing effortlesspromo-ai gateway so its COST is
bounded and its RETRIES are not stacked. It is the AI-gateway analogue of the DCA-125 licensed-provider
adapter — it composes the SAME already-accepted cost machinery around the DCA-128 facade (over the
DCA-129 transport / the dev fixture), never a new gateway or a provider SDK:

- the DCA-113 :class:`~app.cost.reserve.BudgetReservationStore` — a conservative up-front HOLD is
  reserved BEFORE the call; an over-budget request is refused with NO call and NO spend (hard limits deny
  invocation);
- the DCA-115 :class:`~app.cost.provider_call.ProviderCallStore` — the gateway call's lifecycle is
  recorded PENDING → IN_FLIGHT → a terminal state, so a timeout after possible submission (``UNCERTAIN``)
  is never collapsed into a confirmed no-call (``FAILED``);
- the DCA-128 :class:`~app.ai.contract.AiCapabilityFacade` — the ONE chokepoint to the gateway, called
  EXACTLY ONCE under a deterministic ``X-Request-Id`` derived from the stable logical invocation key;
- the DCA-114 :class:`~app.cost.reconcile.BudgetReconciliationStore` — the hold is reconciled from the
  returned ACTUAL usage/cost: SETTLED on a completed call (unused headroom released), RELEASED on a
  confirmed failure, or PRESERVED as ``UNCERTAIN`` (pending) on a timeout — an unknown cost is never
  coerced to zero.

**Retries are bounded, never stacked (the story's one task).** The gateway owns model retry and failover;
this invoker does NOT loop or re-invoke the model. It calls once and *classifies* the outcome with the
accepted DCA-104 discipline (:func:`~app.jobs.retry.classify_failure`) so the SURROUNDING durable-job
layer can decide — bounded by the job's attempt cap — whether to re-attempt. A non-retryable gateway
error (a 4xx rejection / an incompatible-response contract violation) classifies ``PERMANENT`` and is
never retried; a transient one (a 5xx / unreachable gateway) classifies ``TRANSIENT``. A ``UNCERTAIN``
timeout is NOT blindly retried — its reservation stays pending until it is reconciled (DCA-116), and a
replay deliberately reuses the SAME request id against the gateway's idempotency cache rather than
re-executing.

The load-bearing guarantees (the DCA-130 acceptance criteria):

- **Hard limits deny invocation.** An over-budget reservation (or an oversized payload) is refused BEFORE
  the call, so the gateway is never invoked and nothing is spent or written — a denial changes nothing and
  is reproducible.
- **Duplicated receipts cannot double-settle.** The reservation, the provider-call record and the
  reconciliation are each idempotent on a key derived from the stable logical invocation key; and a whole
  re-invocation is an idempotent no-op (the already-terminal provider-call record is rebuilt and returned,
  with no second reserve, no second call and no second settlement).
- **Timeout never implies no spend; an unknown actual cost stays pending rather than zero.** A read/write
  timeout (:class:`~app.clients.ai_client.AiServiceTimeout`) drives the call to ``UNCERTAIN`` and the hold
  to ``MARK_UNCERTAIN`` — the full reserved amount is preserved (it keeps counting against the caps) and is
  neither refunded nor retried, awaiting the DCA-116 resolver.

The invoker stages into the *caller's* session and never commits — the caller owns the unit of work, so
the reservation, the provider-call lifecycle rows, the reconciliation and their atomic DCA-026 audit
entries all commit together. It performs NO real/paid call of its own: the only transport is the DCA-128
facade over the DCA-129 adapter (live only when the gateway is configured — off by default) or the dev
fixture transport (no network, no model, no spend; refused in production).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.ai.contract import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiCapabilityResult,
    AiContractViolation,
    AiUntrustedItem,
    dca_job_binding,
)
from app.audit.sink import ActorType, AuditActor
from app.clients.ai_client import (
    AiServiceRejected,
    AiServiceTimeout,
    AiServiceUnavailable,
)
from app.cost.provider_call import (
    ProviderCallActor,
    ProviderCallIntent,
    ProviderCallRecord,
    ProviderCallStore,
    ProviderCallTransition,
)
from app.cost.reconcile import (
    BudgetReconciliationStore,
    ReconcileOutcome,
    ReconciliationActor,
    ReconciliationRequest,
)
from app.cost.reserve import (
    BudgetReservationStore,
    ReservationActor,
    ReservationError,
    ReservationRequest,
)
from app.db.models import BudgetReservation, ProviderCallStatus, ReservationStatus
from app.identity.permissions import Scope, scopes_for_business_roles
from app.jobs.retry import FailureSignal, JobFailureCategory, JobFailureClass, classify_failure

# The AI capability label / intent recorded on the provider-call lifecycle row for a bound invocation.
DEFAULT_INTENT = "bound_model_invocation"

# The DCA-130 invocation OUTCOME codes (distinct from the DCA-115 lifecycle status). Each is a stable,
# PII-safe marker of what the single gateway call produced.
OUTCOME_COMPLETED = (
    "completed"  # the gateway returned a completed envelope — settled at the actual cost
)
OUTCOME_FAILED = "failed"  # a body-level status=failed envelope — confirmed no spend, hold released
OUTCOME_REJECTED = (
    "rejected"  # a 4xx caller error (bad capability/auth) — non-retryable, hold released
)
OUTCOME_UNAVAILABLE = (
    "unavailable"  # a 5xx / unreachable gateway, NOT submitted — transient, released
)
OUTCOME_UNCERTAIN = (
    "uncertain"  # a read/write timeout, POSSIBLY submitted — preserved pending (DCA-116)
)
OUTCOME_CONTRACT_VIOLATION = (
    "contract_violation"  # an incompatible response — non-retryable, released
)

# Request-shape / gate refusals — each raised/returned BEFORE the call, so a refusal writes nothing.
REASON_MISSING_KEY = "bound_invocation_missing_invocation_key"
REASON_MISSING_CAPABILITY = "bound_invocation_missing_capability"
REASON_INVALID_ESTIMATE = "bound_invocation_invalid_cost_estimate"
REASON_NOT_AUTHORIZED = "bound_invocation_not_authorized"
REASON_PAYLOAD_TOO_LARGE = "bound_invocation_payload_too_large"
REASON_CAPABILITY_DISABLED = "bound_invocation_capability_disabled"


class BoundInvocationError(Exception):
    """A bound-invocation request/shape problem refused the invocation with a stable ``code``. Raised
    before the session is mutated, so a refusal leaves no row and no partial state. ``detail`` names the
    offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class BoundInvocationAuthorizationError(BoundInvocationError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`.

    A bound invocation reserves and reconciles PAID spend, so it is a budget-control action — an ordinary
    acting user and a read-only viewer lack it, and a platform operator holds no business scope for the
    tenant. Raised FIRST (before any state is read or written), so an unauthorized attempt leaves no trace
    and makes no gateway call. ``code`` is always :data:`REASON_NOT_AUTHORIZED`."""

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class BoundInvocationActor:
    """The business principal running a bound model invocation.

    ``subject`` is an opaque actor id (never a name/email) recorded on every ledger row and its atomic
    audit entry. ``roles`` are the caller's verified business-plane membership roles (never a bare token
    claim). :meth:`may_invoke` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` — a bound invocation reserves and reconciles
    spend, so it is an administrator action. The converters hand the SAME identity to each sub-store so the
    reservation / provider-call / reconciliation all attribute to one actor."""

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_invoke(self) -> bool:
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
class BoundInvocationRequest:
    """One would-be bounded model invocation.

    ``invocation_key`` is the STABLE LOGICAL INVOCATION KEY — the single idempotency token from which the
    reservation key, the provider-call key, the reconciliation key and the deterministic gateway
    ``X-Request-Id`` are all derived, so a retry of the whole invocation (and of each sub-step) is a safe
    replay. ``capability`` is the gateway capability to request (must be in the DCA-128 allowlist).
    ``reserved_cost_minor`` is the CONSERVATIVE up-front hold in INR **minor units (paise)** — a positive
    integer, ideally derived from a DCA-112 dry-run quote — reserved before the call. ``input_context`` is
    the MINIMISED, capability-scoped input (never secrets, never raw untrusted text); ``untrusted`` carries
    external text role-separated. ``model_hint`` / ``prompt_version`` are passed through to the gateway
    (which owns routing). ``provider`` names the cost bucket the hold/call are attributed to (defaults to
    the platform bucket). ``intent`` is an opaque purpose code recorded on the provider-call row."""

    invocation_key: str
    capability: str
    reserved_cost_minor: int
    input_context: dict[str, Any] | None = None
    untrusted: tuple[AiUntrustedItem, ...] = ()
    model_hint: str | None = None
    prompt_version: str | None = None
    correlation_id: str | None = None
    provider: str = ""
    intent: str = ""


@dataclass(frozen=True)
class BoundInvocationResult:
    """The deterministic, inert outcome of one bounded invocation (or of an idempotent replay).

    ``authorized`` is the gate verdict; on a denial ``phase`` names which gate decided it
    (``capability`` / ``budget``) and ``reason`` is the stable denial code, with everything else empty
    (nothing reserved, called or settled). ``skipped`` marks an idempotent no-op replay. On a run,
    ``outcome`` is the DCA-130 invocation outcome (one of the ``OUTCOME_*`` codes), ``call_status`` the
    DCA-115 provider-call lifecycle status, and ``gateway_status`` the returned envelope status (``""`` when
    no body arrived). ``reservation_id`` / ``provider_call_id`` are the ledger rows; ``reserved_minor`` the
    hold taken, ``settled_minor`` what it reconciled to (``None`` while preserved as UNCERTAIN),
    ``released_minor`` the freed headroom, ``spent_minor`` the effective bounded spend. ``failure_class`` is
    the DCA-104 classification of a FAILED/uncertain call (``None`` on success); ``failure_category`` the
    PII-safe category behind it; ``needs_review`` flags a preserved uncertain hold; ``structured_output``
    carries a completed call's output."""

    authorized: bool
    capability: str
    invocation_key: str
    provider: str
    business_id: str
    phase: str = ""
    reason: str | None = None
    skipped: bool = False
    outcome: str = ""
    call_status: str = ""
    gateway_status: str = ""
    reservation_id: str | None = None
    provider_call_id: str | None = None
    reserved_minor: int = 0
    settled_minor: int | None = None
    released_minor: int = 0
    spent_minor: int = 0
    failure_class: JobFailureClass | None = None
    failure_category: JobFailureCategory | None = None
    needs_review: bool = False
    structured_output: dict[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == OUTCOME_COMPLETED

    @property
    def retryable(self) -> bool:
        """Whether the SURROUNDING durable-job layer may re-attempt (bounded by its cap) — a transient
        classification. ``False`` on success, on a non-retryable (PERMANENT) error, and when nothing was
        classified. A preserved UNCERTAIN outcome is *transient* but must be reconciled, not blindly
        retried — see :meth:`as_failure_signal`."""
        return self.failure_class is JobFailureClass.TRANSIENT

    def as_failure_signal(
        self, *, retry_after_seconds: float | None = None
    ) -> FailureSignal | None:
        """The DCA-104 :class:`~app.jobs.retry.FailureSignal` for a failed call, or ``None`` on success.

        This is how the "bounded retries" discipline is handed OFF (never stacked here): a surrounding
        durable-job worker feeds this signal to :func:`~app.jobs.retry.plan_retry` / the
        :class:`~app.jobs.retry.JobRetryScheduler`, which bounds the retry by the job's attempt cap and
        dead-letters a PERMANENT failure immediately. The invoker itself calls the gateway exactly once."""
        if self.failure_category is None:
            return None
        return FailureSignal(
            category=self.failure_category,
            error_note=self.outcome,
            retry_after_seconds=retry_after_seconds,
        )


def _amount_to_minor_ceil(amount: float | int | str) -> int:
    """Convert a major-unit money amount to integer minor units, rounding UP (conservative).

    Mirrors the DCA-112 estimator's conversion so a settled actual cost is exact and never an under-count.
    A negative or non-numeric amount is treated as 0 (a completed call that reported no usable cost settles
    to nothing — the hold is released in full), never as a silent over-charge."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return 0
    if value <= 0:
        return 0
    return int((value * 100).to_integral_value(rounding=ROUND_CEILING))


class BoundModelInvoker:
    """Bound ONE model invocation's cost and retries, for ONE business (tenant-scoped).

    Construct with the active session, the verified business scope (a route/worker resolves it from the
    membership-verified tenant context) and the DCA-128 facade over the deployment's transport (the dev
    fixture today; the live DCA-129 adapter only when the gateway is configured). Composes the DCA-113
    reserve → DCA-115 lifecycle → DCA-128 single gateway call → DCA-114 reconcile chain; it stages into the
    caller's session and never commits, and performs no real/paid call of its own.
    """

    def __init__(self, session: Session, business_id: str, facade: AiCapabilityFacade) -> None:
        self._session = session
        self._business_id = business_id
        self._facade = facade
        self._reservations = BudgetReservationStore(session, business_id)
        self._calls = ProviderCallStore(session, business_id)
        self._reconciler = BudgetReconciliationStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def call_for(self, invocation_key: str) -> ProviderCallRecord | None:
        """The provider-call record for one logical invocation in this business, or ``None``."""
        return self._calls.by_key(self._call_key((invocation_key or "").strip()))

    # -- the one invocation path --------------------------------------------------------------------

    def invoke(
        self,
        request: BoundInvocationRequest,
        actor: BoundInvocationActor,
        *,
        max_payload_chars: int = 20_000,
        now: datetime | None = None,
    ) -> BoundInvocationResult:
        """Authorize, bound and run ONE model invocation; return the inert, inspectable result.

        Order (each stage writes nothing on failure): request shape (raises
        :class:`BoundInvocationError`) → authority (raises :class:`BoundInvocationAuthorizationError`) →
        payload-size bound (raises) → capability gate (returns a ``capability`` denial — unknown capability
        or disabled transport) → idempotency skip (an already-run invocation is a deterministic no-op) →
        DCA-113 budget reserve (an over-budget request is refused with NO call — a ``budget`` denial) →
        DCA-115 provider-call lifecycle (PENDING → IN_FLIGHT) → the ONE gateway call via the DCA-128 facade
        → terminal transition + DCA-114 reconcile (settle / release / preserve). The gateway is called
        exactly once (model retry/failover is the gateway's); the outcome is classified for a surrounding
        bounded job retry, never looped here. ``now`` is threaded to every sub-store so a reservation /
        lifecycle / reconciliation share one clock.
        """
        # 1) Request shape — raised before any state is read, so a refusal writes nothing.
        invocation_key = (request.invocation_key or "").strip()
        if not invocation_key:
            raise BoundInvocationError(
                REASON_MISSING_KEY,
                "an invocation_key (the stable logical invocation identity) is required",
            )
        capability = (request.capability or "").strip()
        if not capability:
            raise BoundInvocationError(REASON_MISSING_CAPABILITY, "a capability is required")
        estimate = int(request.reserved_cost_minor)
        if estimate <= 0:
            raise BoundInvocationError(
                REASON_INVALID_ESTIMATE,
                f"reserved_cost_minor must be a positive number of INR minor units (got {estimate})",
                detail=str(estimate),
            )
        provider = (request.provider or "").strip() or "platform"

        # 2) Authority FIRST — a bound invocation reserves/reconciles PAID spend, so it needs BUDGET_MANAGE.
        if not actor.may_invoke():
            raise BoundInvocationAuthorizationError(
                "a bound model invocation reserves and reconciles PAID spend and requires a business "
                f"administrator holding '{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a "
                "read-only viewer or a platform operator (holding no business scope) cannot perform it"
            )

        # 3) Payload-size bound — a bounded invocation never ships an unbounded payload. Measured on the
        #    serialized input context + untrusted frames; an over-limit request is refused BEFORE reserving.
        payload_chars = self._payload_chars(request)
        if payload_chars > max_payload_chars:
            raise BoundInvocationError(
                REASON_PAYLOAD_TOO_LARGE,
                f"invocation payload is {payload_chars} characters, over the {max_payload_chars} bound",
                detail=str(payload_chars),
            )

        # 4) Capability gate (DCA-128, fail-closed) — an unknown capability or a disabled transport is a
        #    reproducible denial that reserves nothing and calls nothing. Future capabilities stay disabled.
        if not self._facade.can_invoke(capability):
            return BoundInvocationResult(
                authorized=False,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                business_id=self._business_id,
                phase="capability",
                reason=REASON_CAPABILITY_DISABLED,
            )

        call_key = self._call_key(invocation_key)
        reserve_key = f"ai:{invocation_key}:reserve"
        request_id = f"dca:{invocation_key}"

        # 5) Idempotency — a logical invocation already run is a deterministic no-op. Checked BEFORE
        #    reserving/calling, so a replay reserves no budget and makes no gateway call.
        existing = self._calls.by_key(call_key)
        if existing is not None:
            return self._skipped_result(existing, invocation_key)

        # 6) Budget reserve (DCA-113) — BEFORE the call. An over-budget request is refused here, so the
        #    gateway is never invoked and nothing is spent or written (hard limits deny invocation).
        try:
            reservation = self._reservations.reserve(
                ReservationRequest(
                    idempotency_key=reserve_key, amount_minor=estimate, provider=provider
                ),
                actor.as_reservation_actor(),
                now=now,
            )
        except ReservationError as exc:
            return BoundInvocationResult(
                authorized=False,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                business_id=self._business_id,
                phase="budget",
                reason=exc.code,
            )
        self._session.flush()  # materialize reservation.id for the provider-call link
        reserved_minor = int(reservation.amount_minor)

        # 7) Provider-call lifecycle (DCA-115) — record the intent PENDING, then dispatch (IN_FLIGHT).
        call_actor = actor.as_provider_call_actor()
        self._calls.record(
            ProviderCallIntent(
                call_key=call_key,
                capability=capability,
                request_id=request_id,
                intent=(request.intent or "").strip() or DEFAULT_INTENT,
                provider=provider,
                model=request.model_hint or "",
                source_version=request.prompt_version or "",
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

        # 8) THE ONE GATEWAY CALL — via the DCA-128 facade only, EXACTLY ONCE (no DCA model-retry loop:
        #    retry/failover is the gateway's). A transport/5xx/timeout error propagates; a body-level
        #    failure comes back as data.
        binding = dca_job_binding(invocation_key, self._business_id)
        ai_request = AiCapabilityRequest(
            capability=capability,
            business_id=self._business_id,
            input_context=dict(request.input_context or {}),
            untrusted=request.untrusted,
            model_hint=request.model_hint,
            prompt_version=request.prompt_version,
            correlation_id=request.correlation_id,
            dry_run=False,
        )
        try:
            result = self._facade.invoke(ai_request, binding)
        except AiServiceTimeout:
            return self._finish_uncertain(
                reservation,
                record,
                call_key,
                call_actor,
                actor,
                now=now,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                reserved_minor=reserved_minor,
                provider_call_id=provider_call_id,
            )
        except AiServiceRejected as exc:
            return self._finish_confirmed_failure(
                reservation,
                call_key,
                call_actor,
                actor,
                now=now,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                reserved_minor=reserved_minor,
                provider_call_id=provider_call_id,
                outcome=OUTCOME_REJECTED,
                outcome_code=f"gateway_rejected_{exc.status_code}",
                category=_reject_category(exc.status_code),
            )
        except AiServiceUnavailable:
            return self._finish_confirmed_failure(
                reservation,
                call_key,
                call_actor,
                actor,
                now=now,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                reserved_minor=reserved_minor,
                provider_call_id=provider_call_id,
                outcome=OUTCOME_UNAVAILABLE,
                outcome_code="gateway_unavailable",
                category=JobFailureCategory.PROVIDER_UNAVAILABLE,
            )
        except (AiCapabilityDisabled, AiContractViolation):
            # An incompatible response (or a capability that just went disabled) is a non-retryable
            # contract problem: confirm no spend, release the hold, classify PERMANENT.
            return self._finish_confirmed_failure(
                reservation,
                call_key,
                call_actor,
                actor,
                now=now,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                reserved_minor=reserved_minor,
                provider_call_id=provider_call_id,
                outcome=OUTCOME_CONTRACT_VIOLATION,
                outcome_code="contract_violation",
                category=JobFailureCategory.NOT_RETRYABLE,
            )

        # 9a) A completed envelope — settle the hold from the RETURNED actual usage/cost.
        if result.succeeded:
            return self._finish_completed(
                reservation,
                call_key,
                call_actor,
                actor,
                result,
                now=now,
                capability=capability,
                invocation_key=invocation_key,
                provider=provider,
                reserved_minor=reserved_minor,
                provider_call_id=provider_call_id,
            )

        # 9b) A body-level status=failed — a legitimate confirmed failure (no spend): release the hold.
        #     Classify from the gateway's own ``retryable`` flag.
        category = (
            JobFailureCategory.PROVIDER_UNAVAILABLE
            if (result.error is not None and result.error.retryable)
            else JobFailureCategory.NOT_RETRYABLE
        )
        return self._finish_confirmed_failure(
            reservation,
            call_key,
            call_actor,
            actor,
            now=now,
            capability=capability,
            invocation_key=invocation_key,
            provider=provider,
            reserved_minor=reserved_minor,
            provider_call_id=provider_call_id,
            outcome=OUTCOME_FAILED,
            outcome_code=(result.error.code if result.error is not None else "gateway_failed"),
            category=category,
        )

    # -- terminal handlers --------------------------------------------------------------------------

    def _finish_completed(
        self,
        reservation: BudgetReservation,
        call_key: str,
        call_actor: ProviderCallActor,
        actor: BoundInvocationActor,
        result: AiCapabilityResult,
        *,
        now: datetime | None,
        capability: str,
        invocation_key: str,
        provider: str,
        reserved_minor: int,
        provider_call_id: str,
    ) -> BoundInvocationResult:
        """SUCCEEDED: record the usage, settle the hold at the returned actual cost (unused headroom freed)."""
        self._calls.transition(
            ProviderCallTransition(
                call_key,
                ProviderCallStatus.SUCCEEDED,
                outcome_code="completed",
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
            ),
            call_actor,
            now=now,
        )
        actual_minor = _amount_to_minor_ceil(result.cost.amount)
        recon = self._reconciler.reconcile(
            ReconciliationRequest(
                reservation_id=reservation.id,
                outcome_key=call_key,
                outcome=ReconcileOutcome.SETTLE,
                actual_minor=actual_minor,
            ),
            actor.as_reconciliation_actor(),
            now=now,
        )
        return BoundInvocationResult(
            authorized=True,
            capability=capability,
            invocation_key=invocation_key,
            provider=provider,
            business_id=self._business_id,
            phase="call",
            outcome=OUTCOME_COMPLETED,
            call_status=ProviderCallStatus.SUCCEEDED.value,
            gateway_status=result.status,
            reservation_id=reservation.id,
            provider_call_id=provider_call_id,
            reserved_minor=reserved_minor,
            settled_minor=recon.settled_minor,
            released_minor=recon.released_minor,
            spent_minor=recon.settled_minor or 0,
            structured_output=result.structured_output,
        )

    def _finish_confirmed_failure(
        self,
        reservation: BudgetReservation,
        call_key: str,
        call_actor: ProviderCallActor,
        actor: BoundInvocationActor,
        *,
        now: datetime | None,
        capability: str,
        invocation_key: str,
        provider: str,
        reserved_minor: int,
        provider_call_id: str,
        outcome: str,
        outcome_code: str,
        category: JobFailureCategory,
    ) -> BoundInvocationResult:
        """A CONFIRMED no-call failure (4xx / 5xx-not-submitted / body-failed / contract violation): mark the
        call FAILED, RELEASE the whole hold (no spend), and classify the failure for a bounded job retry."""
        self._calls.transition(
            ProviderCallTransition(call_key, ProviderCallStatus.FAILED, outcome_code=outcome_code),
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
        return BoundInvocationResult(
            authorized=True,
            capability=capability,
            invocation_key=invocation_key,
            provider=provider,
            business_id=self._business_id,
            phase="call",
            outcome=outcome,
            call_status=ProviderCallStatus.FAILED.value,
            reservation_id=reservation.id,
            provider_call_id=provider_call_id,
            reserved_minor=reserved_minor,
            settled_minor=0,
            released_minor=recon.released_minor,
            spent_minor=0,
            failure_class=classify_failure(category),
            failure_category=category,
        )

    def _finish_uncertain(
        self,
        reservation: BudgetReservation,
        record: ProviderCallRecord,
        call_key: str,
        call_actor: ProviderCallActor,
        actor: BoundInvocationActor,
        *,
        now: datetime | None,
        capability: str,
        invocation_key: str,
        provider: str,
        reserved_minor: int,
        provider_call_id: str,
    ) -> BoundInvocationResult:
        """A read/write TIMEOUT after possible submission: mark the call UNCERTAIN and PRESERVE the whole
        hold (MARK_UNCERTAIN) — a timeout never implies no spend, and the unknown cost stays pending (never
        zero), awaiting the DCA-116 resolver. Never blindly retried."""
        self._calls.transition(
            ProviderCallTransition(
                call_key, ProviderCallStatus.UNCERTAIN, outcome_code="gateway_timeout"
            ),
            call_actor,
            now=now,
        )
        self._reconciler.reconcile(
            ReconciliationRequest(
                reservation_id=reservation.id,
                outcome_key=call_key,
                outcome=ReconcileOutcome.MARK_UNCERTAIN,
            ),
            actor.as_reconciliation_actor(),
            now=now,
        )
        return BoundInvocationResult(
            authorized=True,
            capability=capability,
            invocation_key=invocation_key,
            provider=provider,
            business_id=self._business_id,
            phase="call",
            outcome=OUTCOME_UNCERTAIN,
            call_status=ProviderCallStatus.UNCERTAIN.value,
            reservation_id=reservation.id,
            provider_call_id=provider_call_id,
            reserved_minor=reserved_minor,
            settled_minor=None,  # preserved: the full hold keeps counting toward the caps
            released_minor=0,
            spent_minor=reserved_minor,
            failure_class=classify_failure(JobFailureCategory.TIMEOUT),
            failure_category=JobFailureCategory.TIMEOUT,
            needs_review=True,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _call_key(self, invocation_key: str) -> str:
        return f"ai:{invocation_key}:call"

    def _payload_chars(self, request: BoundInvocationRequest) -> int:
        """The serialized size (characters) of the input context + untrusted frames — the payload bound's
        measure. Pure/no-I/O: a deterministic function of the request."""
        try:
            context_chars = len(
                json.dumps(request.input_context or {}, default=str, sort_keys=True)
            )
        except (TypeError, ValueError):
            # An unserializable context is conservatively measured by its repr (never silently 0).
            context_chars = len(repr(request.input_context))
        untrusted_chars = sum(len(u.source) + len(u.content) for u in request.untrusted)
        return context_chars + untrusted_chars

    def _skipped_result(
        self, record: ProviderCallRecord, invocation_key: str
    ) -> BoundInvocationResult:
        """Rebuild the inert result for an already-run invocation (the idempotent no-op) from the durable
        provider-call row + its reservation — no second reserve, no second call, no second settlement."""
        reservation = (
            self._reservations.get(record.reservation_id) if record.reservation_id else None
        )
        settled_minor, released_minor, spent_minor, needs_review = _ledger_amounts(reservation)
        outcome = _OUTCOME_BY_CALL_STATUS.get(record.status, record.status)
        return BoundInvocationResult(
            authorized=True,
            capability=record.capability,
            invocation_key=invocation_key,
            provider=record.provider,
            business_id=self._business_id,
            phase="call",
            skipped=True,
            outcome=outcome,
            call_status=record.status,
            reservation_id=record.reservation_id,
            provider_call_id=record.id,
            reserved_minor=int(reservation.amount_minor) if reservation is not None else 0,
            settled_minor=settled_minor,
            released_minor=released_minor,
            spent_minor=spent_minor,
            needs_review=needs_review,
        )


# The DCA-130 outcome a terminal DCA-115 lifecycle status maps back to on an idempotent replay. A FAILED
# record is reported as the generic ``failed`` outcome (the specific 4xx/5xx/contract flavour is not
# re-derived on a replay — the ledger effect, a released hold, is identical for all of them).
_OUTCOME_BY_CALL_STATUS: dict[str, str] = {
    ProviderCallStatus.SUCCEEDED.value: OUTCOME_COMPLETED,
    ProviderCallStatus.FAILED.value: OUTCOME_FAILED,
    ProviderCallStatus.UNCERTAIN.value: OUTCOME_UNCERTAIN,
}


def _ledger_amounts(
    reservation: BudgetReservation | None,
) -> tuple[int | None, int, int, bool]:
    """The (settled, released, spent, needs_review) view of a reservation's terminal ledger state —
    mirroring the DCA-125 convention: SETTLED → its settled amount (unused freed); RELEASED → 0 settled,
    whole hold freed; UNCERTAIN → preserved (settled None, spent the full hold, needs review)."""
    if reservation is None:
        return 0, 0, 0, False
    reserved = int(reservation.amount_minor)
    status = reservation.status
    if status == ReservationStatus.SETTLED.value:
        settled = int(
            reservation.settled_amount_minor
            if reservation.settled_amount_minor is not None
            else reserved
        )
        return settled, max(0, reserved - settled), settled, False
    if status == ReservationStatus.RELEASED.value:
        return 0, reserved, 0, False
    if status == ReservationStatus.UNCERTAIN.value:
        return None, 0, reserved, True
    # ACTIVE (a mid-flight record, defensive) — the full hold still counts; nothing settled/released yet.
    return None, 0, reserved, False


def _reject_category(status_code: int) -> JobFailureCategory:
    """Map a 4xx gateway rejection to a PII-safe PERMANENT failure category (authn vs the rest). Every 4xx
    is non-retryable — a blind retry of a caller error would never clear it."""
    if status_code in (401, 403):
        return JobFailureCategory.AUTHENTICATION_FAILED
    return JobFailureCategory.NOT_RETRYABLE


__all__ = [
    "DEFAULT_INTENT",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_REJECTED",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_UNCERTAIN",
    "OUTCOME_CONTRACT_VIOLATION",
    "REASON_MISSING_KEY",
    "REASON_MISSING_CAPABILITY",
    "REASON_INVALID_ESTIMATE",
    "REASON_NOT_AUTHORIZED",
    "REASON_PAYLOAD_TOO_LARGE",
    "REASON_CAPABILITY_DISABLED",
    "BoundInvocationError",
    "BoundInvocationAuthorizationError",
    "BoundInvocationActor",
    "BoundInvocationRequest",
    "BoundInvocationResult",
    "BoundModelInvoker",
]
