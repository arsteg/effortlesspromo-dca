"""DCA-082 — the India privacy-grievance intake/status store (the grievance ledger).

:class:`PrivacyGrievanceStore` is the single sanctioned writer of
:class:`~app.db.models.PrivacyGrievance`. It records ONE grievance a data principal raised with a
business (the data fiduciary), with a PROPORTIONATE verification requirement and a CONFIGURABLE,
REVIEWED response deadline — and exposes a safe status lookup. It does NO erasure, no send and no
model call; acting on a grievance stays a disabled future capability.

The load-bearing guarantees (the DCA-082 acceptance criteria):

- **No contact enumeration; visible only to the authorized requester or operators.** Intake stores the
  requester's contact ONLY as a one-way :func:`contact_digest` (never the raw value) and there is NO
  lookup-by-contact path, so a caller can neither enumerate nor confirm who filed. Status is readable
  two ways and no other: the REQUESTER presents the single claim token issued (once) at intake —
  :meth:`get_for_requester` compares its hash constant-time and returns ``None`` for a wrong/absent
  token, so a bad token reads exactly like not-found (never an existence oracle); or an authorized
  business OPERATOR reads it via the tenant-scoped :meth:`get` / :meth:`grievances` behind the
  ``grievance.read`` gate. Neither projection carries the contact digest or the token hash.

- **A refusal / the relevant denial changes nothing.** The category and channel are validated and the
  claim token derived BEFORE the session is mutated, so a refused intake (an unknown category/channel)
  stages NO row. Each successful record stages an atomic DCA-026 audit entry (opaque references only)
  in the SAME unit of work; the store stages into the caller's session and never commits.

The verification LEVEL is derived deterministically from the category (:data:`ELEVATED_CATEGORIES`),
and every grievance is recorded ``unverified`` / ``received`` — verifying a requester and advancing a
grievance are disabled future capabilities.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    ContactChannel,
    GrievanceCategory,
    GrievanceStatus,
    GrievanceVerificationLevel,
    GrievanceVerificationState,
    PrivacyGrievance,
)

# The controlled vocabulary of categories / channels a grievance may name (empty channel => none given).
GRIEVANCE_CATEGORIES: tuple[str, ...] = tuple(c.value for c in GrievanceCategory)
GRIEVANCE_CHANNELS: tuple[str, ...] = tuple(c.value for c in ContactChannel)

# Categories that expose or alter personal data and therefore demand ELEVATED verification proof —
# acting on a mistaken/forged one of these would itself be a personal-data incident. Proportionality.
ELEVATED_CATEGORIES: frozenset[GrievanceCategory] = frozenset(
    {GrievanceCategory.ACCESS, GrievanceCategory.CORRECTION, GrievanceCategory.ERASURE}
)

# Audited action code for recording a grievance (a controlled code, like the other DCA writes).
AUDIT_ACTION_RECORD_GRIEVANCE = "grievance.record"
# The opaque actor id stamped on the intake audit entry. A public grievance is submitted anonymously
# and relayed by the platform service, so there is no end-user subject — this is an opaque,
# non-contact slug, never a name/email.
INTAKE_ACTOR_ID = "public_grievance_intake"

# Bytes of entropy in the requester's claim token. 32 bytes (256 bits) is ample and unguessable.
_TOKEN_BYTES = 32

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_UNKNOWN_CATEGORY = "unknown_grievance_category"
REASON_UNKNOWN_CHANNEL = "unknown_channel"


class GrievanceIntakeError(Exception):
    """A grievance intake was refused with a stable ``code``. Raised before the session is mutated, so
    a refusal leaves no row and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def contact_digest(value: str) -> str:
    """A one-way, non-reversible digest of a normalized contact — never the raw value.

    Returns ``""`` for an empty contact. The digest exists only so a duplicate submission is
    recognizable; it is never exposed in any response and there is no path that looks a grievance up by
    it, so it cannot be used to enumerate or confirm contacts.
    """
    token = (value or "").strip().lower()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    """The SHA-256 hex digest of a claim token — the only form of it ever persisted."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def verification_level_for(category: GrievanceCategory) -> GrievanceVerificationLevel:
    """The PROPORTIONATE verification level a category requires (deterministic, never free input)."""
    if category in ELEVATED_CATEGORIES:
        return GrievanceVerificationLevel.ELEVATED
    return GrievanceVerificationLevel.STANDARD


def _normalize_category(value: str) -> GrievanceCategory:
    token = (value or "").strip().lower()
    try:
        return GrievanceCategory(token)
    except ValueError as exc:
        raise GrievanceIntakeError(
            REASON_UNKNOWN_CATEGORY,
            f"unknown grievance category '{value}'; expected one of: "
            + ", ".join(GRIEVANCE_CATEGORIES),
            detail=value,
        ) from exc


def _normalize_channel(value: str) -> str:
    """Normalize a channel; "" (none supplied) passes through, a non-empty value must be known."""
    token = (value or "").strip().lower()
    if not token:
        return ""
    try:
        return ContactChannel(token).value
    except ValueError as exc:
        raise GrievanceIntakeError(
            REASON_UNKNOWN_CHANNEL,
            f"unknown channel '{value}'; expected one of: {', '.join(GRIEVANCE_CHANNELS)} (or empty)",
            detail=value,
        ) from exc


@dataclass
class GrievanceIntakeInput:
    """One grievance as supplied by the caller (a public form relayed by the platform).

    ``category`` is required and must be a known :class:`~app.db.models.GrievanceCategory`. ``contact``
    is the requester's contact value — used ONLY to derive the one-way digest and then discarded; it is
    never stored raw. ``channel`` (when set) must be a known channel. ``detail`` is the requester's own
    description (optional, tenant-owned, never surfaced in a status/list projection).
    """

    category: str
    contact: str = ""
    channel: str = ""
    detail: str = ""


@dataclass(frozen=True)
class IssuedGrievance:
    """The result of a successful intake: the (uncommitted) grievance row plus the ONE-TIME claim token.

    ``access_token`` is the plaintext token the requester uses to look up status; it is returned a
    single time here and never stored (only its hash lives on the row), so it cannot be recovered later.
    """

    grievance: PrivacyGrievance
    access_token: str


class PrivacyGrievanceStore:
    """Tenant-scoped reader/writer for privacy grievances, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business — there is no argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, grievance_id: str) -> PrivacyGrievance | None:
        """Fetch one grievance by id WITHIN this business (the operator read). Another business's id
        (or an unknown id) resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not grievance_id:
            return None
        stmt = (
            select(PrivacyGrievance)
            .where(PrivacyGrievance.business_id == self._business_id)
            .where(PrivacyGrievance.id == grievance_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def get_for_requester(self, grievance_id: str, access_token: str) -> PrivacyGrievance | None:
        """Fetch one grievance only if the presented claim token matches (the REQUESTER read).

        Returns ``None`` when the grievance is not in this business OR the token does not match — a
        wrong/absent token reads exactly like not-found, so it is never an existence oracle. The hash
        comparison is constant-time (:func:`hmac.compare_digest`), so it leaks no timing signal.
        """
        grievance = self.get(grievance_id)
        if grievance is None:
            return None
        presented = _hash_token(access_token or "")
        if not hmac.compare_digest(presented, grievance.access_token_hash):
            return None
        return grievance

    def grievances(self) -> list[PrivacyGrievance]:
        """Every grievance for this business, oldest first — the authorized-operator ledger."""
        stmt = (
            select(PrivacyGrievance)
            .where(PrivacyGrievance.business_id == self._business_id)
            .order_by(PrivacyGrievance.created_at, PrivacyGrievance.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one write path -------------------------------------------------------------------------

    def record(
        self,
        data: GrievanceIntakeInput,
        *,
        response_deadline_days: int,
        now: datetime | None = None,
    ) -> IssuedGrievance:
        """Validate and stage a NEW grievance; return it plus the one-time claim token (uncommitted).

        The category/channel are validated and the claim token derived BEFORE the session is mutated,
        so a refused intake (unknown category/channel) raises :class:`GrievanceIntakeError` and stages
        NO row. The proportionate verification level is derived from the category; the grievance is
        recorded ``unverified`` / ``received``. The REVIEWED ``response_deadline_days`` is pinned onto
        the row and ``respond_by`` computed from it, so the deadline is reproducible. An atomic DCA-026
        audit entry (opaque references only — never the contact or the token) is staged in the SAME
        unit of work. The caller owns the transaction.
        """
        moment = now or datetime.now(UTC)
        category = _normalize_category(data.category)
        channel = _normalize_channel(data.channel)
        level = verification_level_for(category)

        # Mint the requester's claim token (unguessable) and persist only its hash — the plaintext is
        # returned once and never stored.
        access_token = secrets.token_urlsafe(_TOKEN_BYTES)

        grievance = PrivacyGrievance(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            category=category.value,
            channel=channel,
            requester_contact_digest=contact_digest(data.contact),
            detail=(data.detail or "").strip(),
            verification_level_required=level.value,
            verification_state=GrievanceVerificationState.UNVERIFIED.value,
            status=GrievanceStatus.RECEIVED.value,
            response_deadline_days=response_deadline_days,
            respond_by=moment + timedelta(days=response_deadline_days),
            access_token_hash=_hash_token(access_token),
            created_at=moment,
        )
        self._session.add(grievance)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — id / category / level / deadline — never the contact or the token.
        AuditSink(
            self._session,
            business_id=self._business_id,
            actor=AuditActor(id=INTAKE_ACTOR_ID, type=ActorType.SERVICE),
        ).record(
            action=AUDIT_ACTION_RECORD_GRIEVANCE,
            reason=f"recorded privacy grievance '{grievance.id}' ({category.value})",
            references={
                "grievanceId": grievance.id,
                "category": category.value,
                "verificationLevelRequired": level.value,
                "verificationState": GrievanceVerificationState.UNVERIFIED.value,
                "status": GrievanceStatus.RECEIVED.value,
                "responseDeadlineDays": response_deadline_days,
            },
        )
        return IssuedGrievance(grievance=grievance, access_token=access_token)
