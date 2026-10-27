"""DCA-057 — persist policy decision evidence.

:class:`PolicyDecisionRecordStore` is the single sanctioned writer of
:class:`~app.db.models.PolicyDecisionRecord`. It takes the result of the deterministic DCA-055/056
policy evaluator (a :class:`~app.compliance.policy_engine.PolicyEvaluation`) and persists it as a
durable, auditable record so one decision can be *reviewed and reproduced independently* — traceable
to the exact reviewed rule/evidence set, bounded by an explicit expiry, with safe reason codes and the
relevant source/consent/suppression version tags it was taken under.

The load-bearing guarantees (the DCA-057 acceptance criteria):

- **A stored ALLOW is traceable to the exact reviewed rule/evidence set and can NEVER imply permanent
  authorization.** The record pins the in-force ``rulepack_version`` + ``approval_id`` (the DCA-053
  review), the engine version, the evidence actually consulted and the immutable, content-fingerprinted
  DCA-056 snapshot. :meth:`PolicyDecisionRecordStore.record` REFUSES to persist an ``ALLOW`` without a
  non-null ``expires_at`` (:data:`REASON_ALLOW_REQUIRES_EXPIRY`) and refuses an expiry at/before the
  evaluation moment (:data:`REASON_INVALID_EXPIRY`), so a stored allow always lapses with the review it
  rests on — it is never a standing grant. :meth:`PolicyDecisionRecordStore.is_in_force` returns True
  only for an ALLOW evaluated BEFORE its expiry.

- **The decision reproduces from the row ALONE, with no state change.**
  :meth:`PolicyDecisionRecordStore.reproduce` rebuilds the request and replays the stored snapshot (or,
  for a fail-closed denial whose snapshot is ``None``, re-decides with no pack at the recorded moment)
  to yield the identical decision — even after the live pack has changed — reading only the row and
  writing nothing (it opens no transaction and consults no model).

- **Scoped, versioned, audited.** The store is bound to one ``business_id``; every read/write is
  confined to it (defence-in-depth RLS backs the app-level scope on Postgres). Each ``record`` stages
  an atomic DCA-026 audit entry (opaque references only) in the SAME unit of work as the row, and the
  store stages — never commits — into the caller's session, so the record and its audit entry commit
  together.

The convenience :func:`evaluate_and_record` ties the pieces together for a caller that wants to
evaluate a request and persist the outcome in one scoped unit of work: it resolves the in-force pack
via :class:`~app.compliance.policy_engine.PolicyEvaluator`, derives the decision's expiry from the
DCA-053 approval that put the pack in force (so a recorded ALLOW's expiry IS the review expiry — it can
never outlive its review), then records it. Recording a decision enforces nothing: the DCA has no
send/acquisition path, so future capabilities stay disabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.compliance.policy_engine import (
    PolicyDecision,
    PolicyEvaluation,
    PolicyEvaluator,
    PolicyRequest,
    decide,
    replay,
    request_from_dict,
    request_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
)
from app.db.models import PolicyDecisionRecord
from app.services.policy_approval import PolicyApprovalStore

# The audited action code for persisting a policy decision (a controlled code, like the other writes).
AUDIT_ACTION_RECORD_DECISION = "policy.decision.record"

# Stable refusal codes (raised BEFORE the session is mutated, so a refusal leaves no row). Both guard
# the "a stored ALLOW cannot imply permanent authorization" criterion.
REASON_ALLOW_REQUIRES_EXPIRY = "allow_requires_expiry"
REASON_INVALID_EXPIRY = "invalid_expiry"


class PolicyDecisionRecordError(Exception):
    """A decision record was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class PolicyDecisionContext:
    """The relevant input *versions* a decision was taken under (DCA-057).

    These are opaque, declarative version tags — never recipient contact data — recorded alongside the
    decision so it can be tied to the exact inputs that governed it:

    - ``consent_version`` — the consent-basis/record version the request's consent facts came from.
    - ``suppression_version`` — the suppression-list version checked (suppression is enforced by the
      platform's send pipeline; the DCA has no send path — this merely tags which version was relevant).
    - ``source_versions`` — per-source terms versions relied on, ``{source_key: version}``.
    - ``extra`` — any further opaque version tags a caller wants retained.
    """

    consent_version: str = ""
    suppression_version: str = ""
    source_versions: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def as_references(self) -> dict:
        """Render the context as the opaque ``references`` dict stored on the record."""
        refs: dict = {}
        if self.consent_version:
            refs["consent_version"] = self.consent_version
        if self.suppression_version:
            refs["suppression_version"] = self.suppression_version
        if self.source_versions:
            refs["source_versions"] = dict(self.source_versions)
        if self.extra:
            refs.update(dict(self.extra))
        return refs


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class PolicyDecisionRecordStore:
    """Tenant-scoped reader/writer for persisted policy decisions, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business; there is no argument to widen the scope. The store stages into the caller's session and
    never commits — the caller owns the unit of work, so the record and its atomic audit entry commit
    together.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, record_id: str) -> PolicyDecisionRecord | None:
        """One decision record within this business, or ``None`` (a cross-tenant/unknown id reads as
        not-found, never surfaced)."""
        stmt = (
            select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.business_id == self._business_id)
            .where(PolicyDecisionRecord.id == record_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def list(self) -> list[PolicyDecisionRecord]:
        """All decision records for this business, oldest first."""
        stmt = (
            select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.business_id == self._business_id)
            .order_by(PolicyDecisionRecord.created_at, PolicyDecisionRecord.id)
        )
        return list(self._session.scalars(stmt).all())

    def latest(self) -> PolicyDecisionRecord | None:
        """The most recently persisted decision for this business, or ``None``."""
        stmt = (
            select(PolicyDecisionRecord)
            .where(PolicyDecisionRecord.business_id == self._business_id)
            .order_by(PolicyDecisionRecord.created_at.desc(), PolicyDecisionRecord.id.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one write path -------------------------------------------------------------------------

    def record(
        self,
        evaluation: PolicyEvaluation,
        request: PolicyRequest,
        *,
        actor: AuditActor,
        expires_at: datetime | None = None,
        context: PolicyDecisionContext | None = None,
    ) -> PolicyDecisionRecord:
        """Persist ONE policy decision; return the record (uncommitted — caller owns the UoW).

        Enforces the "no permanent authorization" invariant FIRST (before any row is staged): an
        ``ALLOW`` evaluation MUST carry a non-null ``expires_at`` (else
        :class:`PolicyDecisionRecordError` ``allow_requires_expiry``), and any supplied expiry must be
        strictly after the evaluation moment (else ``invalid_expiry``). The row captures the request
        facts + the immutable DCA-056 snapshot (so the decision reproduces from the row alone), the
        exact reviewed rulepack version / approval / engine version / evidence, safe reason codes, and
        the relevant source/consent/suppression version tags (``context``). Stages an atomic DCA-026
        audit entry (opaque references only) in the SAME unit of work.
        """
        # The evaluation moment is the snapshot's instant when present; otherwise it is "now" (a
        # fail-closed denial taken before any pack was consulted has no snapshot). Parsed from the
        # snapshot's ISO string where available so replay re-decides at exactly the recorded instant.
        evaluated_at = self._evaluation_moment(evaluation)
        expires = _as_utc(expires_at)

        if evaluation.decision is PolicyDecision.ALLOW and expires is None:
            raise PolicyDecisionRecordError(
                REASON_ALLOW_REQUIRES_EXPIRY,
                "a stored ALLOW must carry an explicit expiry so it cannot imply permanent "
                "authorization",
            )
        if expires is not None and expires <= evaluated_at:
            raise PolicyDecisionRecordError(
                REASON_INVALID_EXPIRY,
                "the decision expiry must be strictly after the evaluation moment",
                detail=f"{evaluated_at.isoformat()}..{expires.isoformat()}",
            )

        refs = context.as_references() if context is not None else {}
        record = PolicyDecisionRecord(
            business_id=self._business_id,
            jurisdiction=evaluation.jurisdiction,
            action=evaluation.action,
            channel=evaluation.channel,
            decision=evaluation.decision.value,
            reason_codes=list(evaluation.reasons),
            rulepack_version=evaluation.rulepack_version,
            approval_id=evaluation.approval_id,
            engine_version=evaluation.engine_version,
            decisive_authority=evaluation.decisive_authority,
            evidence=[dict(e) for e in evaluation.evidence],
            references=refs,
            request_facts=dict(request.facts or {}),
            involves_personal_data=request.involves_personal_data,
            snapshot=snapshot_to_dict(evaluation.snapshot) if evaluation.snapshot else None,
            snapshot_fingerprint=(
                evaluation.snapshot.fingerprint if evaluation.snapshot else ""
            ),
            evaluated_at=evaluated_at,
            expires_at=expires,
            actor_id=actor.id,
            actor_type=actor.type.value,
        )
        self._session.add(record)

        # Atomic audit (SEC-001/BR-122 discipline): staged into the caller's session so it commits with
        # the record. References are opaque (decision code, reason codes, versions) — no contact data.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_RECORD_DECISION,
            reason=f"policy decision '{evaluation.decision.value}' recorded",
            references={
                "action": evaluation.action,
                "channel": evaluation.channel,
                "jurisdiction": evaluation.jurisdiction,
                "decision": evaluation.decision.value,
                "reasonCodes": list(evaluation.reasons),
                "rulepackVersion": evaluation.rulepack_version,
                "approvalId": evaluation.approval_id,
                "engineVersion": evaluation.engine_version,
                "snapshotFingerprint": (
                    evaluation.snapshot.fingerprint if evaluation.snapshot else ""
                ),
                "expiresAt": expires.isoformat() if expires is not None else None,
            },
        )
        return record

    # -- reproduction (pure; no DB write, no model) -------------------------------------------------

    @staticmethod
    def _evaluation_moment(evaluation: PolicyEvaluation) -> datetime:
        from app.compliance.policy_engine import _parse_dt  # local: shared ISO→UTC parser

        if evaluation.snapshot is not None:
            parsed = _parse_dt(evaluation.snapshot.evaluated_at)
            if parsed is not None:
                return parsed
        return datetime.now(UTC)

    @staticmethod
    def rebuild_request(record: PolicyDecisionRecord) -> PolicyRequest:
        """Rebuild the exact :class:`PolicyRequest` a record was taken for, from the row alone."""
        return request_from_dict(
            {
                "action": record.action,
                "channel": record.channel,
                "jurisdiction": record.jurisdiction,
                "involves_personal_data": record.involves_personal_data,
                "facts": dict(record.request_facts or {}),
            }
        )

    @staticmethod
    def reproduce(record: PolicyDecisionRecord) -> PolicyEvaluation:
        """Reproduce a recorded decision from the row ALONE — no DB, no live pack, no state change.

        When the row carries a snapshot (any pack-backed decision), replay it; when it does not (a
        fail-closed denial taken before any pack was consulted — e.g. an unresolved jurisdiction or no
        in-force pack), re-decide with no pack at the recorded moment. Either path is pure and yields
        the identical decision the row records, even if the live rulepack has since changed.
        """
        from app.compliance.policy_engine import _parse_dt

        request = PolicyDecisionRecordStore.rebuild_request(record)
        if record.snapshot:
            return replay(request, snapshot_from_dict(dict(record.snapshot)))
        moment = _parse_dt(record.evaluated_at) or datetime.now(UTC)
        return decide(request, None, now=moment)

    @staticmethod
    def is_in_force(record: PolicyDecisionRecord, *, now: datetime | None = None) -> bool:
        """True only for an ALLOW evaluated BEFORE its recorded expiry.

        A stored ALLOW authorizes nothing past ``expires_at`` (it lapses with the review it rests on),
        and no other outcome ever authorizes — so this is the one safe "may this still be relied on?"
        check. A decision has no send/acquisition effect regardless; this merely reports freshness.
        """
        if record.decision != PolicyDecision.ALLOW.value:
            return False
        expires = _as_utc(record.expires_at)
        if expires is None:
            return False
        moment = now or datetime.now(UTC)
        return moment < expires


def _resolve_decision_expiry(
    session: Session, evaluation: PolicyEvaluation, *, jurisdiction: str
) -> datetime | None:
    """Derive a decision's expiry from the DCA-053 approval that put the in-force pack in force.

    A pack-backed decision rests on an approval with an explicit review expiry; pinning the decision's
    expiry to it means a recorded ALLOW can never outlive the review that authorized it. Returns the
    approval's ``expires_at`` (tz-normalized) when the evaluation names an approved version, else
    ``None`` (a fail-closed denial has no approval and needs no expiry).
    """
    if evaluation.rulepack_version is None:
        return None
    approval = PolicyApprovalStore(session).approval_for(
        evaluation.rulepack_version, jurisdiction
    )
    return _as_utc(approval.expires_at) if approval is not None else None


def evaluate_and_record(
    session: Session,
    business_id: str,
    request: PolicyRequest,
    *,
    actor: AuditActor,
    context: PolicyDecisionContext | None = None,
    now: datetime | None = None,
) -> tuple[PolicyEvaluation, PolicyDecisionRecord]:
    """Evaluate one request and persist the decision in one scoped unit of work (uncommitted).

    Resolves the in-force pack via :class:`~app.compliance.policy_engine.PolicyEvaluator`, derives the
    decision's expiry from the DCA-053 approval that put the pack in force (so a recorded ALLOW's expiry
    IS the review expiry — it can never outlive its review), then records it. Returns the evaluation and
    the staged record. The caller owns the transaction; recording a decision enforces nothing (the DCA
    has no send/acquisition path).
    """
    evaluator = PolicyEvaluator(session)
    evaluation = evaluator.evaluate(request, now=now)
    jurisdiction = (request.jurisdiction or "").strip().upper()
    expires_at = _resolve_decision_expiry(session, evaluation, jurisdiction=jurisdiction)
    record = PolicyDecisionRecordStore(session, business_id).record(
        evaluation, request, actor=actor, expires_at=expires_at, context=context
    )
    return evaluation, record
