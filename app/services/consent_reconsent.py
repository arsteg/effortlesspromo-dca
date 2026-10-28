"""DCA-077 — the re-consent (withdrawn -> granted) command, gated on FRESH evidence.

:class:`ConsentReconsentService` is the one write path that RESTORES a permission the subject
previously withdrew. It is the sibling of the DCA-074 grant writer
(:mod:`app.services.consent_receipt`), the DCA-075 state projector
(:mod:`app.consent.permission_state`) and the DCA-076 withdrawal command
(:mod:`app.services.consent_withdrawal`): recording a grant, deriving the state, withdrawing, and
re-consenting are deliberately independent capabilities, each reviewable on its own.

Re-consent is NOT a normal grant. After a withdrawal the only thing that may restore permission is a
NEW, VALID, AFFIRMATIVE receipt — a genuinely fresh opt-in. Before appending anything, the command
runs the pure DCA-077 gate (:func:`app.consent.reconsent.evaluate_reconsent`) over the scope's existing
timeline, so the three ways a withdrawn permission could be made to *look* re-granted are refused and
stage NO row (the DCA-077 acceptance criteria):

- a **replayed original receipt** (reusing the original opt-in's captured evidence) is refused as stale;
- a **stale import** (a grant collected at/before the in-force withdrawal) is refused as not-after;
- a **provider refresh** (no fresh captured evidence / reused proof) is refused as missing/stale.

Only when the gate reports ``restored`` does the command append a fresh GRANT receipt via
:meth:`app.services.consent_receipt.ConsentReceiptStore.record` (which independently re-validates the
cited evidence, contact point and channel WITHIN this business), emit a
:data:`OUTBOX_EVENT_SCOPE_RECONSENTED` propagation intent, and return the resulting projection (now
``GRANTED``). The receipt, its atomic DCA-026 audit entry and the outbox intent all commit together in
the caller's unit of work; a refused re-consent changes nothing. Recording a re-consent authorizes no
send and creates no prospect / host record — acting on the restored permission stays a disabled future
capability behind the one send pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit.sink import AuditActor
from app.consent.permission_state import (
    PermissionEvent,
    PermissionProjection,
    derive_permission_state,
    events_from_receipts,
)
from app.consent.reconsent import ReconsentDecision, evaluate_reconsent
from app.outbox.sink import OutboxSink
from app.services.consent_receipt import (
    ConsentReceiptError,
    ConsentReceiptInput,
    ConsentReceiptStore,
)

# The outbox event emitted when a scope is re-consented — a propagation INTENT, relayed by a later
# story. Distinct from the grant/withdraw events so the trail shows a re-consent as its own event.
OUTBOX_EVENT_SCOPE_RECONSENTED = "consent.scope.reconsented"

# The affirmative actions a re-consent receipt may record (a withdraw/decline cannot re-grant).
_AFFIRMATIVE_ACTIONS = ("grant", "confirm")


class ConsentReconsentError(Exception):
    """A re-consent was refused with a stable ``code``. Raised BEFORE the session is mutated, so a
    refusal stages no receipt and no outbox event. ``detail`` names the offending value when useful
    (never a raw contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReconsentInput:
    """The fresh affirmative receipt proposed to restore a withdrawn scope.

    Mirrors :class:`~app.services.consent_receipt.ConsentReceiptInput` (the scope + the captured proof),
    but ``action`` must be an affirmative action and ``evidence_id`` must cite a NEW, same-business
    :class:`~app.db.models.EvidenceObject` — the proof of the fresh opt-in. ``collected_at`` is when the
    subject acted and must be STRICTLY AFTER the in-force withdrawal.
    """

    brand_key: str
    contact_point_id: str
    channel: str
    purpose: str
    action: str
    notice_version: str
    form_version: str
    language: str
    evidence_id: str
    collected_at: datetime


class ConsentReconsentService:
    """Re-consent one withdrawn permission scope for one business, gated on fresh evidence.

    Construct with the active session and the VERIFIED business scope (a route resolves it from the
    membership-checked tenant context, never a bare header). Every read/write is confined to that
    business. The service never opens, commits or rolls back — the caller owns the unit of work, which
    is what makes the fresh grant, its audit entry and its outbox intent atomic.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def reconsent(
        self,
        data: ReconsentInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> PermissionProjection:
        """Restore ``data``'s scope with a fresh affirmative receipt, or refuse and change nothing.

        Reads the scope's existing timeline within this business, runs the pure DCA-077 gate, and — only
        when it reports ``restored`` — appends a fresh GRANT receipt and a re-consent outbox intent,
        returning the resulting ``GRANTED`` projection. A refused re-consent (replayed original receipt,
        stale import, or provider refresh) raises :class:`ConsentReconsentError` with the gate's stable
        code BEFORE the session is mutated, so it stages no row.
        """
        moment = now or datetime.now(UTC)
        store = ConsentReceiptStore(self._session, self._business_id)

        action = (data.action or "").strip().lower()
        if action not in _AFFIRMATIVE_ACTIONS:
            raise ConsentReconsentError(
                "reconsent_not_affirmative",
                f"re-consent requires an affirmative action (one of: "
                f"{', '.join(_AFFIRMATIVE_ACTIONS)}); got '{data.action}'",
                detail=data.action,
            )

        # The scope's existing receipt history (this business only), restricted to the candidate scope.
        brand_key = (data.brand_key or "").strip().lower()
        channel = (data.channel or "").strip().lower()
        purpose = (data.purpose or "").strip().lower()
        prior_rows = [
            r
            for r in store.for_contact_point((data.contact_point_id or "").strip())
            if r.brand_key == brand_key and r.channel == channel and r.purpose == purpose
        ]
        prior_events = events_from_receipts(prior_rows)

        # The candidate as the gate sees it (not yet recorded, so it carries no receipt id).
        candidate = PermissionEvent(
            receipt_id="",
            brand_key=brand_key,
            contact_point_id=(data.contact_point_id or "").strip(),
            channel=channel,
            purpose=purpose,
            action=action,
            collected_at=data.collected_at,
            recorded_at=moment,
            evidence_id=(data.evidence_id or "").strip(),
        )

        # The DCA-077 gate decides BEFORE anything is staged — a refusal changes nothing.
        decision: ReconsentDecision = evaluate_reconsent(prior_events, candidate, now=moment)
        if not decision.restored:
            raise ConsentReconsentError(
                decision.reason,
                f"re-consent refused ({decision.reason}): a withdrawn permission can only be "
                "restored by a new, affirmative receipt citing FRESH evidence collected after the "
                "withdrawal",
                detail=decision.reason,
            )

        # Append the fresh GRANT receipt. The store re-validates the scope + evidence WITHIN this
        # business (contact point exists, channel matches, evidence resolves) and stages its atomic
        # audit entry; a store refusal surfaces as the command's error type and stages nothing.
        try:
            receipt = store.record(
                ConsentReceiptInput(
                    brand_key=data.brand_key,
                    contact_point_id=data.contact_point_id,
                    channel=data.channel,
                    purpose=data.purpose,
                    action=action,
                    notice_version=data.notice_version,
                    form_version=data.form_version,
                    language=data.language,
                    evidence_id=data.evidence_id,
                    collected_at=data.collected_at,
                ),
                actor=actor,
                now=moment,
            )
        except ConsentReceiptError as exc:
            raise ConsentReconsentError(exc.code, str(exc), detail=exc.detail) from exc

        # Transactional outbox intent — opaque references only (no raw contact value). Staged into the
        # same session so it commits atomically with the receipt; a later relay propagates it.
        OutboxSink(self._session, business_id=self._business_id).enqueue(
            event_type=OUTBOX_EVENT_SCOPE_RECONSENTED,
            dedup_key=f"reconsent:{receipt.id}",
            payload={
                "receiptId": receipt.id,
                "brandKey": receipt.brand_key,
                "subjectType": receipt.subject_type,
                "subjectId": receipt.subject_id,
                "contactPointId": receipt.contact_point_id,
                "channel": receipt.channel,
                "purpose": receipt.purpose,
                "action": receipt.action,
            },
        )

        # The resulting in-force state for this scope — proof the restoration is immediate. The store
        # hasn't flushed yet, so include the just-staged receipt alongside the committed timeline.
        timeline = list(prior_rows)
        timeline.append(receipt)
        return derive_permission_state(events_from_receipts(timeline), now=moment)
