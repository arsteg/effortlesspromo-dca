"""DCA-158 — deliver a recorded BILLABLE acceptance to EffortlessPromo platform billing.

The audience stage's metering-delivery step after DCA-157 recorded a durable
:class:`~app.db.models.BillableAcceptance` (status ``recorded``) for an accepted prospect whose audience
sync was ACKNOWLEDGED. This service hands that usage FACT to the DCA-158
:class:`~app.billing.contract.UsageBillingPort` — against the synthetic fake today; the live adapter into
the platform's billing module stays a disabled future capability (DCA-001 B1) and production refuses it
outright (:func:`~app.billing.fake.build_billing_usage_port`). Delivering hands the platform a minimized
:class:`~app.billing.contract.UsageRecord` under the SAME stable request key the entry was recorded under
(its ``billing_key``), and applies the platform's :class:`~app.billing.contract.UsageReceipt` back onto the
entry. The DCA meters nothing, prices nothing and CHARGES NOTHING — the platform owns billing.

The load-bearing invariants (the DCA-158 acceptance criteria):

- **Repeated delivery cannot double-bill.** ``recorded → delivered`` is advanced ONLY on an ACKNOWLEDGED
  receipt (a host usage-record handle present). An already-``delivered`` entry short-circuits — no second
  port call, no second meter, no second audit — so a replay (or a second drain pass) returns the existing
  delivery unchanged. The request key is the entry's STABLE ``billing_key`` (deterministic in business +
  intent), so a retried delivery is the SAME logical usage and the host returns the SAME handle.

- **A missing configured price invents no charge; an unacknowledged delivery is never success.** A delivery
  whose meter has no configured price (or whose host is unavailable / stale) comes back DISABLED: the entry
  stays ``recorded``, no handle / price is persisted and NOTHING is charged. A PENDING hand-off (a lost /
  timed-out response) likewise leaves the entry ``recorded`` (retried later) — the status never advances on
  anything short of an acknowledgment.

- **Provider cost stays distinct from the customer price.** The usage record MAY carry the DCA's own
  ``provider_cost_minor`` (an internal cost basis, never a charge); the customer price is minted by the
  platform and only ever read back FROM the receipt. The two are recorded as distinct values and the DCA
  never sets the customer price.

Every guard is checked fail-closed, in order, BEFORE any state is mutated, so a refusal (unauthorized /
not-found / production-disabled) changes nothing. The service stages into the *caller's* session and never
commits — the route/worker owns the unit of work, so the status advance, the DCA-027 usage record's
delivered transition and the atomic DCA-026 audit entry commit (or roll back) together. There is no
argument to widen the business scope: an unknown / cross-tenant entry reads as not-found.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.billing import (
    UsageBillingPort,
    UsageDeliveryRequest,
    UsageMeter,
    UsageReceipt,
    UsageRecord,
    build_billing_usage_port,
    usage_record_from_entry,
)
from app.db.models import (
    BillableAcceptance,
    BillableAcceptanceStatus,
    OutboxMessage,
    OutboxStatus,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.outbox.sink import OutboxSink

# The audited action code for applying a usage-delivery result onto a billable acceptance (controlled,
# distinct from the DCA-157 ``billable_accepted`` emit code so delivery is a separate audited dimension).
AUDIT_ACTION_DELIVERY = "billing.usage.delivered"

# Where a usage delivery records its origin on the provenance / audit references.
USAGE_DELIVERY_SOURCE = "usage_delivery"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any state
# is mutated, so the attempt changes nothing. The not-found code reads identically for an unknown or a
# cross-tenant id (a delivery can never reach another tenant's entry).
REASON_NOT_AUTHORIZED = "usage_delivery_not_authorized"
REASON_ENTRY_NOT_FOUND = "billable_acceptance_not_found"


class UsageDeliveryError(Exception):
    """A usage delivery was refused with a stable ``code`` for a malformed / ineligible request (an unknown
    or cross-tenant billable acceptance). Raised before the session is mutated, so a refusal changes
    nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class UsageDeliveryAuthorizationError(UsageDeliveryError):
    """The caller is not authorized to deliver usage to platform billing.

    A distinct type so an HTTP boundary can map it to a 403 while the other code maps to a 404. Raised
    FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class UsageDeliveryActor:
    """The business principal delivering usage to platform billing.

    ``subject`` is an opaque actor id (never a name/email) recorded on the atomic audit entry. ``roles`` are
    the caller's verified business-plane membership roles (from the tenant context, NEVER a bare token
    claim). :meth:`may_deliver` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT` — delivering the already-recorded billable fact
    is the same business authority as accepting / syncing / metering the prospect; a read-only viewer and a
    platform operator (holding no business scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_deliver(self) -> bool:
        return Scope.CANDIDATE_ACCEPT in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class UsageDeliveryResult:
    """The inspectable outcome of one usage-delivery attempt.

    ``delivered`` is ``True`` when this call advanced the entry ``recorded → delivered`` (an ACKNOWLEDGED
    meter); ``already_delivered`` when the entry was already delivered and this was an idempotent no-op (no
    second port call / meter / audit). ``accepted`` is whether the platform TOOK the usage (PENDING or
    ACKNOWLEDGED); ``acknowledged`` whether it DURABLY metered it. ``receipt`` is the platform's
    :class:`~app.billing.contract.UsageReceipt` (``None`` only on an idempotent replay, where no port is
    called). ``customer_price_minor`` is the platform's price (present only when acknowledged — the DCA
    never sets it); ``provider_cost_minor`` is the DCA's own cost basis carried on the delivery — DISTINCT
    values. ``status`` echoes the entry's ledger status after the attempt. ``reason`` is a stable code.
    Nothing here is ever charged by the DCA or consented — ``charged_by_dca`` / ``confers_consent`` are
    always ``False``.
    """

    business_id: str
    entry: BillableAcceptance
    status: str
    delivered: bool
    already_delivered: bool
    accepted: bool
    acknowledged: bool
    reason: str
    receipt: UsageReceipt | None = None
    customer_price_minor: int | None = None
    provider_cost_minor: int = 0
    currency: str = "INR"

    @property
    def charged_by_dca(self) -> bool:
        """The DCA charges NOTHING — the platform owns billing. Always ``False``."""
        return False

    @property
    def confers_consent(self) -> bool:
        """Delivering usage confers NO recipient consent (acceptance is NOT consent). Always ``False``."""
        return False


class UsageBillingDeliverer:
    """Deliver recorded BILLABLE acceptances to EffortlessPromo platform billing, for ONE business.

    Construct with the active session, the verified business scope and the deployment settings (used to
    resolve the usage-billing port via the DCA-158 build factory, which refuses production). Every query is
    confined to this business — there is no argument to widen the scope — so a delivery can never reach
    another tenant's entry, and an unknown / cross-tenant id reads as not-found. The deliverer stages into
    the caller's session and never commits; it advances an entry ``recorded → delivered`` only on an
    acknowledgment and charges / consents / sends nothing.
    """

    def __init__(
        self,
        session: Session,
        business_id: str,
        settings: Any,
        *,
        port: UsageBillingPort | None = None,
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._settings = settings
        # An injected port is a dev/test seam (to drive the fake's acknowledge / priced-meter knobs);
        # production still resolves through the build factory below, which refuses the fake outright.
        self._port = port

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def _entry(self, entry_id: str) -> BillableAcceptance | None:
        """Fetch one billable acceptance by id WITHIN this business, or ``None`` — an unknown or
        cross-tenant id reads exactly like not-found (a delivery can never target another tenant's row)."""
        if not entry_id:
            return None
        stmt = (
            select(BillableAcceptance)
            .where(BillableAcceptance.business_id == self._business_id)
            .where(BillableAcceptance.id == entry_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def recorded_entries(self, *, limit: int = 100) -> list[BillableAcceptance]:
        """The ``recorded`` (not-yet-delivered) billable acceptances for this business, oldest first — the
        drain's work list. Tenant-scoped; never reaches another tenant's rows."""
        stmt = (
            select(BillableAcceptance)
            .where(BillableAcceptance.business_id == self._business_id)
            .where(BillableAcceptance.status == BillableAcceptanceStatus.RECORDED.value)
            .order_by(BillableAcceptance.created_at.asc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def _resolve_port(self) -> UsageBillingPort:
        """Resolve the usage-billing port. Production is refused through the DCA-158 build factory (the live
        adapter is unbuilt, B1); otherwise an injected test port or the synthetic fake is used."""
        if getattr(self._settings, "is_production", False):
            # Honour the contract's production refusal even if a port was injected — the disabled-in-
            # production rule is absolute (build_billing_usage_port raises BillingUsageContractDisabled).
            return build_billing_usage_port(self._settings)
        return self._port or build_billing_usage_port(self._settings)

    # -- the one delivery path ----------------------------------------------------------------------

    def deliver_entry(
        self,
        entry_id: str,
        actor: UsageDeliveryActor,
        *,
        meter: UsageMeter = UsageMeter.ACCEPTED_PROSPECT,
        provider_cost_minor: int = 0,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> UsageDeliveryResult:
        """Deliver ONE recorded billable acceptance to platform billing, for this business.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT`
           is refused (:class:`UsageDeliveryAuthorizationError`) before any state is read.
        2. **The entry exists in THIS business.** Otherwise refused (:data:`REASON_ENTRY_NOT_FOUND`) — an
           unknown or cross-tenant id reads as not-found.
        3. **Idempotent replay.** An entry already ``delivered`` (its host handle persisted) returns
           unchanged — no port call, no second meter, no second audit — so a replay never double-bills.
        4. **Deliver + apply.** The port (production-refused via the build factory) is asked to deliver the
           usage record (built from the entry under its stable ``billing_key``); the receipt is applied back
           onto the entry — ACKNOWLEDGED advances it to ``delivered`` and marks its DCA-027 usage record
           delivered; a PENDING / DISABLED receipt leaves it ``recorded`` (retried, no charge invented).
           Both stage an atomic DCA-026 audit entry in the caller's unit of work.

        Delivering performs NO send, confers NO recipient consent and the DCA CHARGES NOTHING.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_deliver():
            raise UsageDeliveryAuthorizationError(
                "delivering usage to platform billing requires a business user holding "
                f"'{Scope.CANDIDATE_ACCEPT.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The entry must exist WITHIN this business (an unknown / cross-tenant id is not-found).
        entry = self._entry(entry_id)
        if entry is None:
            raise UsageDeliveryError(
                REASON_ENTRY_NOT_FOUND,
                "no such billable acceptance in this business (an unknown or cross-tenant id is "
                "not-found)",
                detail=entry_id,
            )

        # 3) Idempotent replay — an already-delivered entry returns its persisted handle unchanged.
        if entry.status == BillableAcceptanceStatus.DELIVERED.value and entry.delivery_receipt_id:
            prov = entry.provenance if isinstance(entry.provenance, dict) else {}
            return UsageDeliveryResult(
                business_id=self._business_id,
                entry=entry,
                status=entry.status,
                delivered=False,
                already_delivered=True,
                accepted=True,
                acknowledged=True,
                reason="ok",
                customer_price_minor=prov.get("customerPriceMinor"),
                provider_cost_minor=int(prov.get("providerCostMinor", 0) or 0),
                currency=prov.get("currency", "INR"),
            )

        # 4) Build the usage record under the entry's stable billing key, resolve the port (production-
        #    refused here, before any mutation) and deliver.
        record = usage_record_from_entry(
            entry,
            meter=meter,
            provider_cost_minor=provider_cost_minor,
            correlation_id=correlation_id,
        )
        port = self._resolve_port()
        request = UsageDeliveryRequest(
            request_key=entry.billing_key,
            record=record,
            correlation_id=correlation_id,
        )
        receipt = port.deliver(request)
        return self._apply(entry, record, receipt, actor, moment)

    def deliver_recorded(
        self,
        actor: UsageDeliveryActor,
        *,
        limit: int = 100,
        meter: UsageMeter = UsageMeter.ACCEPTED_PROSPECT,
        provider_cost_minor: int = 0,
        now: datetime | None = None,
    ) -> list[UsageDeliveryResult]:
        """Drain the business's ``recorded`` billable acceptances: deliver each via the port and advance the
        acknowledged ones to ``delivered``. Authority is checked once (FIRST); an unauthorized caller
        delivers nothing. Returns one :class:`UsageDeliveryResult` per processed entry. Idempotent — a
        re-run only re-touches entries still ``recorded`` (an acknowledged entry is skipped by the work
        list), so it never double-bills."""
        if not actor.may_deliver():
            raise UsageDeliveryAuthorizationError(
                "delivering usage to platform billing requires a business user holding "
                f"'{Scope.CANDIDATE_ACCEPT.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )
        results: list[UsageDeliveryResult] = []
        for entry in self.recorded_entries(limit=limit):
            results.append(
                self.deliver_entry(
                    entry.id,
                    actor,
                    meter=meter,
                    provider_cost_minor=provider_cost_minor,
                    now=now,
                )
            )
        return results

    # -- internals ----------------------------------------------------------------------------------

    def _apply(
        self,
        entry: BillableAcceptance,
        record: UsageRecord,
        receipt: UsageReceipt,
        actor: UsageDeliveryActor,
        moment: datetime,
    ) -> UsageDeliveryResult:
        """Apply one :class:`~app.billing.contract.UsageReceipt` onto the entry + stage its atomic audit.
        ACKNOWLEDGED advances the entry to ``delivered``, persists the platform's handle + price and marks
        the DCA-027 usage record delivered; a PENDING / DISABLED receipt leaves it ``recorded`` (retried, no
        charge invented). Delivery attempts are counted either way (inspectable, benign bookkeeping)."""
        acknowledged = receipt.is_acknowledged
        entry.last_delivery_attempt_at = moment
        entry.delivery_attempt_count = (entry.delivery_attempt_count or 0) + 1

        if acknowledged:
            assert (
                receipt.host_reference
            )  # the receipt model guarantees a handle exactly when acknowledged
            entry.status = BillableAcceptanceStatus.DELIVERED.value
            entry.delivered_at = moment
            entry.delivery_receipt_id = receipt.host_reference
            # Flip ONLY the 'delivered' invariant; delivery is still never a DCA charge / send / consent. The
            # provider cost (the DCA's basis) and the platform's customer price are recorded as DISTINCT
            # values — never conflated — and the customer price is read FROM the receipt (never set by us).
            entry.provenance = {
                **(entry.provenance or {}),
                "delivered": True,
                "deliverySource": USAGE_DELIVERY_SOURCE,
                "deliveryReceiptId": receipt.host_reference,
                "meter": record.meter.value,
                "providerCostMinor": record.provider_cost_minor,
                "customerPriceMinor": receipt.customer_price_minor,
                "currency": receipt.currency,
                "chargedByDca": False,
            }
            # Complete the DCA-027 usage record's lifecycle: a durable PENDING hand-off becomes DELIVERED in
            # the SAME unit of work, so the ledger entry and its outbox record settle together.
            self._mark_usage_record_delivered(entry, moment)
        # A PENDING hand-off (lost / timed-out) or a DISABLED refusal (unpriced meter / unavailable host)
        # leaves the entry 'recorded': no handle, no price, no charge invented, nothing marked delivered.

        self._session.flush()

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the status
        # advance. References stay OPAQUE — the entry / intent / billing key, the platform's handle, the
        # DISTINCT provider cost vs customer price — never a contact value. Delivery is never a DCA charge.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_DELIVERY,
            reason=(
                f"applied {receipt.status.value} usage delivery for billable acceptance '{entry.id}' "
                "(no DCA charge, no send, no consent)"
            ),
            references={
                "billableAcceptanceId": entry.id,
                "acceptanceIntentId": entry.acceptance_intent_id,
                "billingKey": entry.billing_key,
                "meter": record.meter.value,
                "requestKey": receipt.request_key,
                "deliveryStatus": receipt.status.value,
                "accepted": receipt.accepted,
                "acknowledged": acknowledged,
                "hostReference": receipt.host_reference or None,
                # The two money values are recorded SEPARATELY so a provider cost can never read as a charge.
                "providerCostMinor": record.provider_cost_minor,
                "customerPriceMinor": receipt.customer_price_minor,
                "currency": receipt.currency,
                "ledgerStatus": entry.status,
                "synthetic": receipt.synthetic,
                "chargedByDca": False,
                "consentConferred": False,
                "sent": False,
            },
        )

        return UsageDeliveryResult(
            business_id=self._business_id,
            entry=entry,
            status=entry.status,
            delivered=acknowledged,
            already_delivered=False,
            accepted=receipt.accepted,
            acknowledged=acknowledged,
            reason=receipt.reason,
            receipt=receipt,
            customer_price_minor=receipt.customer_price_minor,
            provider_cost_minor=record.provider_cost_minor,
            currency=receipt.currency,
        )

    def _mark_usage_record_delivered(self, entry: BillableAcceptance, moment: datetime) -> None:
        """Advance the DCA-027 billing-usage outbox record staged with this entry PENDING → DELIVERED, in
        the caller's unit of work. A no-op if the record is absent or already delivered."""
        if not entry.outbox_event_id:
            return
        stmt = (
            select(OutboxMessage)
            .where(OutboxMessage.business_id == self._business_id)
            .where(OutboxMessage.event_id == entry.outbox_event_id)
        )
        message = self._session.scalars(stmt).one_or_none()
        if message is None or message.status == OutboxStatus.DELIVERED.value:
            return
        OutboxSink(self._session, business_id=self._business_id).mark_delivered(message, at=moment)


__all__ = [
    "AUDIT_ACTION_DELIVERY",
    "USAGE_DELIVERY_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_ENTRY_NOT_FOUND",
    "UsageDeliveryError",
    "UsageDeliveryAuthorizationError",
    "UsageDeliveryActor",
    "UsageDeliveryResult",
    "UsageBillingDeliverer",
]
