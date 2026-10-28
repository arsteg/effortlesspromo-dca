"""DCA-083 — resolve a privacy request's affected scope (the operator-reviewed scope resolver).

Key: ``privacy-scope`` · source sections 5, 10, 11.

Recording a privacy request (a DCA-082 :class:`~app.db.models.PrivacyGrievance`, or a DCA-078
:class:`~app.db.models.PlatformSourceRestriction`) only logs the request — it does not, by itself, say
WHICH records the request reaches, WHO the controllers/fiduciaries of those records are, or WHAT a
handler would be permitted to do about them. This story is that resolution step: a READ-ONLY analysis
an authorized operator reviews BEFORE anyone acts. It identifies the affected records, the
controllers/fiduciaries and the permissible actions — and **executes nothing** (no deletion, no send,
no model call, no state change of any kind). Acting on the resolved scope (erasing, correcting,
removing source copies) stays a DISABLED future capability.

Two resolution planes mirror the two privacy-request kinds — and keep the *scope* boundary exact:

- :meth:`PrivacyScopeResolver.resolve_local` — a LOCAL privacy request (a grievance a data principal
  raised with ONE business, the data fiduciary). Bound to that grievance's business, it identifies ONLY
  that business's records that pertain to the requester (matched by the requester's one-way contact
  digest — never a raw contact), names that single business as the data fiduciary, and derives the
  permissible action from the grievance category. It can never reach another tenant: a request for one
  tenant does NOT automatically select every tenant's relationship. Where the requester's data was
  acquired from a platform-controlled source, whether that subject's data elsewhere on the platform is
  in scope is left UNRESOLVED and flagged for review — resolving the platform-wide scope requires a
  platform operator's separate authority (see below). Mirrors the tenant-scoped authority of
  :class:`~app.services.grievance_intake.PrivacyGrievanceStore`.

- :meth:`PrivacyScopeResolver.resolve_platform_source` — a PLATFORM-SOURCE removal request (a
  justified, platform-wide removal of a catalogued source, DCA-078). Keyed on the control-plane source,
  it identifies EVERY tenant's records acquired from the removed source, naming the platform as the
  source controller and each affected tenant as a data fiduciary. This is the platform-plane scope a
  local operator has no authority to resolve — mirroring the platform-plane authority of the DCA-081
  :class:`~app.services.restriction_fanout.RestrictionFanout`.

**Authority first, and a denial changes nothing.** Each method takes the reviewing principal and checks
its authority BEFORE any state is read, so a relevant denial is pure and reproducible:

- ``resolve_local`` requires a business operator whose membership roles grant
  :attr:`~app.identity.permissions.Scope.GRIEVANCE_READ` (a read-only viewer and a platform operator
  both lack it for the tenant); and
- ``resolve_platform_source`` requires a platform operator holding
  :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT` (a tenant administrator never does).

The authority plane is also cross-checked against the restriction row, so a platform-plane removal can
only be resolved on the platform plane. Resolution is pure read: it opens no transaction, writes no
row and performs no I/O, so the result and the relevant denial both reproduce with no state change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    GrievanceCategory,
    PlatformSourceRestriction,
    PrivacyGrievance,
    ProspectCandidate,
    RestrictionAuthorityPlane,
)
from app.identity.permissions import Scope, scopes_for_business_roles, scopes_for_platform_roles
from app.services.grievance_intake import contact_digest

# The request kinds a resolution answers (recorded on the result so the two are told apart).
REQUEST_KIND_LOCAL = "local_grievance"
REQUEST_KIND_PLATFORM_SOURCE = "platform_source_removal"

# The controller/fiduciary roles a resolution names (opaque, descriptive; never a person).
CONTROLLER_DATA_FIDUCIARY = "data_fiduciary"
CONTROLLER_SOURCE_CONTROLLER = "source_controller"

# The candidate contact field names that carry a reachable contact value (the only keys digested and
# compared to a grievance's requester digest). A minimised candidate contact (DCA-058) holds opaque
# field names like ``email`` / ``phone``; other keys (``kind`` / ``role``) are never treated as contacts.
_CONTACT_FIELDS: frozenset[str] = frozenset({"email", "phone", "mobile", "landline", "whatsapp"})

# The permissible action a grievance CATEGORY identifies — what a handler would be permitted to do about
# the affected records. Identifying an action NEVER executes it: every resolution is non-executable here
# (``actions_executable`` is always False), so even an erasure request only ever NAMES ``erase_personal_data``
# and deletes nothing. Acting on any of these stays a disabled future capability.
PERMISSIBLE_ACTIONS: dict[GrievanceCategory, tuple[str, ...]] = {
    GrievanceCategory.ACCESS: ("provide_access_summary",),
    GrievanceCategory.CORRECTION: ("correct_personal_data",),
    GrievanceCategory.ERASURE: ("erase_personal_data",),
    GrievanceCategory.CONSENT_WITHDRAWAL: ("withdraw_consent",),
    GrievanceCategory.NOMINATION: ("record_nomination",),
    GrievanceCategory.PROCESSING_COMPLAINT: ("review_processing",),
    GrievanceCategory.OTHER: ("manual_review",),
}
# The permissible action a platform-source removal identifies (removing the platform-controlled copies).
PLATFORM_SOURCE_ACTIONS: tuple[str, ...] = ("remove_platform_source_copies",)

# Stable review/denial codes. Authority codes are raised before any state is read.
REASON_NOT_AUTHORIZED = "privacy_scope_not_authorized"
REASON_NOT_PLATFORM_OPERATOR = "not_platform_operator"
REASON_PLANE_MISMATCH = "restriction_authority_plane_mismatch"
# Review flags surfaced on a resolution (``unresolved_authority``) — scope a reviewer must NOT treat as
# resolved because it needs separate authority. They are advisory items on a successful result, not errors.
REVIEW_SUBJECT_CONTACT_NOT_PROVIDED = "subject_contact_not_provided"
REVIEW_PLATFORM_SOURCE_SCOPE = "platform_source_scope"  # suffixed with ":{source_key}"


class PrivacyScopeError(Exception):
    """A scope resolution was refused with a stable ``code``. Raised before any state is read, so a
    refusal reads nothing and changes nothing. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class PrivacyScopeAuthorizationError(PrivacyScopeError):
    """The caller is not authorized to resolve this scope.

    A distinct type so an HTTP boundary can map it to a 403. Raised before anything is read. ``code`` is
    :data:`REASON_NOT_AUTHORIZED` (local) or :data:`REASON_NOT_PLATFORM_OPERATOR` (platform-wide).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class GrievanceScopePrincipal:
    """The business principal resolving a LOCAL grievance's scope.

    ``subject`` is an opaque actor id (never a name/email). ``roles`` are the caller's business-plane
    membership roles (from the verified :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare
    token claim). :meth:`may_resolve` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.GRIEVANCE_READ` — a read-only viewer's roles do not, and a
    platform operator holds no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_resolve(self) -> bool:
        return Scope.GRIEVANCE_READ in scopes_for_business_roles(self.roles)


@dataclass(frozen=True)
class PlatformScopePrincipal:
    """The platform principal resolving a PLATFORM-SOURCE removal's scope.

    ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal — never a
    bare header). :meth:`may_resolve_platform` grants the action only when these roles yield
    :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT`; a tenant administrator's business
    roles never do.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_resolve_platform(self) -> bool:
        return Scope.PLATFORM_SOURCE_RESTRICT in scopes_for_platform_roles(self.roles)


@dataclass(frozen=True)
class AffectedRecord:
    """One record a privacy request reaches — OPAQUE references only (ids / status / source key).

    Carries no org name and no contact value: a scope resolution identifies WHICH records are in scope
    without re-exposing the personal data they hold.
    """

    record_id: str
    business_id: str
    source_key: str
    status: str


@dataclass(frozen=True)
class Controller:
    """A controller/fiduciary of the affected records. ``role`` is :data:`CONTROLLER_DATA_FIDUCIARY`
    (a business that holds the records) or :data:`CONTROLLER_SOURCE_CONTROLLER` (the platform, for the
    removed source). Exactly one of ``business_id`` / ``source_key`` is set for the role."""

    role: str
    business_id: str = ""
    source_key: str = ""


@dataclass
class ScopeResolution:
    """The resolved affected scope of one privacy request — a review artifact, never an execution.

    ``affected_records`` are the records identified (opaque), ``controllers`` the fiduciaries/controllers
    of those records, and ``permissible_actions`` what a handler would be PERMITTED to do — none of which
    is executed here (``actions_executable`` is always ``False``: execution, including deletion, stays a
    disabled future capability). ``unresolved_authority`` lists scope a reviewer must NOT treat as
    resolved because it needs separate authority (so :attr:`needs_review` is ``True``): a missing subject
    contact, or platform-controlled source scope a local operator cannot resolve.
    """

    request_kind: str
    request_id: str
    authority_plane: str
    subject_digest: str = ""
    source_key: str = ""
    affected_records: list[AffectedRecord] = field(default_factory=list)
    controllers: list[Controller] = field(default_factory=list)
    permissible_actions: list[str] = field(default_factory=list)
    # Execution of any resolved action — ALWAYS disabled here. Resolving a scope deletes/sends nothing.
    actions_executable: bool = False
    unresolved_authority: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """True when some scope could not be resolved under the caller's authority (a reviewer must
        decide it separately), e.g. platform-controlled scope surfaced by a local resolution."""
        return bool(self.unresolved_authority)

    @property
    def record_count(self) -> int:
        return len(self.affected_records)

    @property
    def affected_business_ids(self) -> list[str]:
        """The distinct tenants whose records are in scope, sorted (one for a local request; possibly
        many for a platform-source removal)."""
        return sorted({r.business_id for r in self.affected_records})


class PrivacyScopeResolver:
    """The read-only resolver of a privacy request's affected scope (DCA-083).

    Performs pure reads over the caller's session: it never flushes, commits, opens a transaction or
    writes a row, so the result and any relevant denial both reproduce with no state change.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- subject matching (local) -------------------------------------------------------------------

    @staticmethod
    def _candidate_matches_subject(candidate: ProspectCandidate, subject_digest: str) -> bool:
        """True when any of the candidate's contact values digests to the grievance's subject digest.

        The grievance stores the requester's contact only as a one-way digest; here each candidate
        contact value is digested the SAME way (:func:`~app.services.grievance_intake.contact_digest`)
        and compared — so a record is matched to the subject without ever handling a raw contact. An
        empty subject digest matches nothing (there is no subject to resolve)."""
        if not subject_digest:
            return False
        contact = candidate.contact or {}
        for key, value in contact.items():
            if str(key).strip().lower() not in _CONTACT_FIELDS:
                continue
            if value and contact_digest(str(value)) == subject_digest:
                return True
        return False

    # -- the LOCAL resolution (tenant-scoped) -------------------------------------------------------

    def resolve_local(
        self, grievance: PrivacyGrievance, principal: GrievanceScopePrincipal
    ) -> ScopeResolution:
        """Resolve the affected scope of ONE business's privacy grievance; return it (read-only).

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.GRIEVANCE_READ` is refused with
        :class:`PrivacyScopeAuthorizationError` before any state is read (the relevant denial). On
        success the resolver identifies ONLY this grievance's business records that pertain to the
        requester (matched by the one-way contact digest), names that single business as the data
        fiduciary, and derives the permissible action from the category — executing nothing. A request
        for one tenant never selects another tenant's records; where a matched record came from a
        platform-controlled source, that platform-wide scope is left UNRESOLVED
        (:data:`REVIEW_PLATFORM_SOURCE_SCOPE`) for a platform operator to resolve separately, and a
        grievance with no subject contact flags :data:`REVIEW_SUBJECT_CONTACT_NOT_PROVIDED`.
        """
        if not principal.may_resolve():
            raise PrivacyScopeAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "resolving a grievance's affected scope requires a business operator holding "
                f"'{Scope.GRIEVANCE_READ.value}'; a read-only viewer or a non-member cannot perform it, "
                "and a platform operator holds no business scope for this tenant",
            )

        resolution = ScopeResolution(
            request_kind=REQUEST_KIND_LOCAL,
            request_id=grievance.id,
            authority_plane=RestrictionAuthorityPlane.BUSINESS.value,
            subject_digest=grievance.requester_contact_digest or "",
            controllers=[
                Controller(role=CONTROLLER_DATA_FIDUCIARY, business_id=grievance.business_id)
            ],
            permissible_actions=list(_permissible_actions_for(grievance.category)),
        )

        if not grievance.requester_contact_digest:
            # No subject to resolve against — the affected set stays empty and the gap is flagged for
            # review rather than guessed (a resolution never over-selects).
            resolution.unresolved_authority.append(REVIEW_SUBJECT_CONTACT_NOT_PROVIDED)
            return resolution

        # ONLY this business's records (explicit tenant filter) — a one-tenant request never widens to
        # another tenant's relationships.
        candidates = self._session.scalars(
            select(ProspectCandidate).where(ProspectCandidate.business_id == grievance.business_id)
        ).all()

        platform_sources: set[str] = set()
        for candidate in candidates:
            if not self._candidate_matches_subject(candidate, grievance.requester_contact_digest):
                continue
            resolution.affected_records.append(
                AffectedRecord(
                    record_id=candidate.id,
                    business_id=candidate.business_id,
                    source_key=candidate.source_key or "",
                    status=candidate.status,
                )
            )
            if candidate.source_key:
                platform_sources.add(candidate.source_key)

        # A matched record acquired from a platform-controlled source means the same subject may have
        # platform-controlled copies elsewhere on the platform. A local (business-plane) operator has no
        # authority to resolve that platform-wide scope, so it is surfaced as needs-review — never
        # auto-selected across tenants.
        for source_key in sorted(platform_sources):
            resolution.unresolved_authority.append(f"{REVIEW_PLATFORM_SOURCE_SCOPE}:{source_key}")
        return resolution

    # -- the PLATFORM-SOURCE resolution (spans tenants) ---------------------------------------------

    def resolve_platform_source(
        self, restriction: PlatformSourceRestriction, principal: PlatformScopePrincipal
    ) -> ScopeResolution:
        """Resolve the affected scope of a platform-wide source removal; return it (read-only).

        Authorization is checked FIRST: only a platform operator holding
        :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT` may resolve a platform-wide
        removal — a tenant administrator is refused before any state is read. The restriction must be a
        PLATFORM-plane row (a local grievance is resolved via :meth:`resolve_local`). On success the
        resolver identifies EVERY tenant's records acquired from the removed source, names the platform
        as the source controller and each affected tenant as a data fiduciary, and identifies the
        removal action — executing nothing (no copy is removed). The full platform-wide scope is within
        the operator's authority, so nothing is left unresolved.
        """
        if not principal.may_resolve_platform():
            raise PrivacyScopeAuthorizationError(
                REASON_NOT_PLATFORM_OPERATOR,
                "resolving a platform-wide source removal's scope requires a platform operator "
                f"('{Scope.PLATFORM_SOURCE_RESTRICT.value}'); a tenant administrator cannot perform it",
            )
        if restriction.authority_plane != RestrictionAuthorityPlane.PLATFORM.value:
            raise PrivacyScopeError(
                REASON_PLANE_MISMATCH,
                "resolve_platform_source expects a platform-plane restriction; a local grievance is "
                "resolved via resolve_local",
                detail=restriction.authority_plane,
            )

        resolution = ScopeResolution(
            request_kind=REQUEST_KIND_PLATFORM_SOURCE,
            request_id=restriction.id,
            authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
            source_key=restriction.source_key,
            controllers=[
                Controller(role=CONTROLLER_SOURCE_CONTROLLER, source_key=restriction.source_key)
            ],
            permissible_actions=list(PLATFORM_SOURCE_ACTIONS),
        )

        # Across ALL tenants (keyed on the control-plane source) — the platform-plane read the operator
        # is authorized for, mirroring the DCA-081 platform-wide fanout.
        candidates = self._session.scalars(
            select(ProspectCandidate)
            .where(ProspectCandidate.source_key == restriction.source_key)
            .order_by(ProspectCandidate.business_id, ProspectCandidate.id)
        ).all()

        for candidate in candidates:
            resolution.affected_records.append(
                AffectedRecord(
                    record_id=candidate.id,
                    business_id=candidate.business_id,
                    source_key=candidate.source_key or "",
                    status=candidate.status,
                )
            )
        # One data-fiduciary controller per affected tenant (the platform controls the source; each
        # tenant is the fiduciary of its own copy).
        for business_id in resolution.affected_business_ids:
            resolution.controllers.append(
                Controller(role=CONTROLLER_DATA_FIDUCIARY, business_id=business_id)
            )
        return resolution


def _permissible_actions_for(category_value: str) -> tuple[str, ...]:
    """The permissible action(s) a grievance category identifies. An unrecognized category (defensive —
    the store validates categories at intake) falls back to manual review; it never executes anything."""
    try:
        category = GrievanceCategory(category_value)
    except ValueError:
        return PERMISSIBLE_ACTIONS[GrievanceCategory.OTHER]
    return PERMISSIBLE_ACTIONS[category]
