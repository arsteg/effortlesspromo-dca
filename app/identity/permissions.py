"""DCA-019 — operation permissions: explicit scopes across two disjoint planes.

Authorization here is by *explicit scope*, never a universal-admin bypass. There are two planes and
they never overlap:

- **Business plane** — scopes a subject holds *inside one business*, derived ONLY from that business's
  membership roles (the DCA-016 verified :class:`~app.identity.tenant_context.TenantContext`). An
  ordinary business user holds read-level scopes; approving sources/policies or changing budgets is
  an administrator scope. A token's self-asserted roles can never grant a business scope (DCA-016).
- **Platform plane** — scopes a platform operator holds over the *platform itself*. A platform
  operator gets platform scopes only; it gets NO business scope for any tenant, so global operator
  rights never authorize a client-business action. Acting *as* a client (delegated tenant support)
  requires a separate, scoped, expiring grant — a later story — and stays disabled here. Approving a
  shared source *configuration* (its catalog entry + terms, DCA-046) lives HERE on the platform plane
  (:attr:`Scope.SOURCE_TERMS_APPROVE`): the catalog is platform control-plane data shared across all
  tenants, so a tenant administrator can never approve it — the business ``source.approve`` scope
  below is a business-local intent, not authority over the shared catalog.

The two scope sets are disjoint, so :func:`scopes_for_business_roles` can never return a platform
scope and :func:`scopes_for_platform_roles` can never return a business scope — crossing a plane is
structurally impossible, not merely unchecked.
"""

from __future__ import annotations

from enum import Enum


class Scope(str, Enum):
    """A single, explicitly-granted capability. Checks test membership in a scope set — there is no
    wildcard and no role that implies "everything"."""

    # --- business plane (granted via business membership roles only) ---
    CANDIDATE_READ = "candidate.read"  # ordinary business user: view suggestions
    # correct/override a field value the agent recorded about one of this business's subjects
    # (DCA-070). A WRITE scope: an ordinary acting business user holds it, a read-only viewer does
    # NOT — correcting a value is an edit, not a view. It is a business-plane scope, so a platform
    # operator (holding no membership) can never correct a client's field; delegated client-support
    # correction would be a separate, scoped, expiring grant (a later story), never an operator right.
    FIELD_CORRECT = "field.correct"
    # approve an ambiguous identity merge — record that two of THIS business's observed identities
    # (two OrgObservations or two PersonObservations) are the SAME, on top of a reviewed COMPATIBLE
    # matcher conclusion (DCA-071). A WRITE scope: an ordinary acting business user holds it (approving
    # a surfaced merge candidate is a normal reviewing action, not admin-only), a read-only viewer does
    # NOT. It is a business-plane scope, so a platform operator (holding no membership) can never merge a
    # client's identities with their global rights alone. A merge is a HUMAN review ACTION: the pure
    # matchers (DCA-067/068) only ever conclude MATCH/REVIEW and merge nothing, so an AI suggestion /
    # automation actor can never execute one even if it somehow carried a role — the reviewer must be a
    # human user. Acting on a merged identity downstream stays a disabled future capability.
    IDENTITY_MERGE = "identity.merge"
    # withdraw (restrict) ONE permission scope the agent holds for one of this business's contact
    # points (DCA-076). A WRITE scope: an ordinary acting business user holds it (honouring an opt-out
    # is a normal operating action, never admin-only), a read-only viewer does NOT. It is a
    # business-plane scope, so a platform operator (holding no membership) can never withdraw a
    # client's permission with their global rights alone; the subject's OWN one-click withdrawal takes
    # the separate public-token path, which carries its own authority and needs no business role.
    CONSENT_WITHDRAW = "consent.withdraw"
    # place a LOCAL (tenant) processing restriction on a scope this business owns — a brand/channel/
    # purpose, or the whole tenant (DCA-078). A WRITE scope: an ordinary acting business user holds it
    # (restricting the agent's own processing for a scope is a normal operating action, never admin-
    # only), a read-only viewer does NOT. It is a business-plane scope, so a platform operator (holding
    # no membership) can never place a client's restriction with their global rights alone; a
    # platform-WIDE source removal is the separate platform scope below, never this one.
    PROCESSING_RESTRICT = "processing.restrict"
    # read the privacy grievances (DCA-082) raised with THIS business, and look up one's status as an
    # authorized handler/operator. A READ scope held by an acting business user and an administrator
    # (handling grievances is a normal operating action for a designated handler), NOT a read-only
    # viewer. It is a business-plane scope, so a platform operator (holding no membership) can never
    # read a client's grievances with their global rights alone; the REQUESTER's own status lookup takes
    # the separate claim-token path, which carries its own authority and needs no business role.
    GRIEVANCE_READ = "grievance.read"
    # erase the DCA's OWN operational data (observations/evidence/projections) for ONE of this
    # business's subjects, in response to a reviewed privacy obligation (DCA-084). An ADMINISTRATOR
    # scope: erasing personal data is irreversible and sensitive, so an ordinary acting user and a
    # read-only viewer both lack it — only a business administrator (admin/owner) holds it. It is a
    # business-plane scope, so a platform operator (holding no membership) can never erase a client's
    # data with their global rights alone; delegated client-support erasure would be a separate,
    # scoped, expiring grant (a later story), never an Admin-role-alone right.
    DATA_ERASE = "data.erase"
    SOURCE_APPROVE = "source.approve"  # administrator: approve a data source
    POLICY_APPROVE = "policy.approve"  # administrator: approve a compliance policy
    BUDGET_MANAGE = "budget.manage"  # administrator: change spend budgets
    # store a per-business recurring schedule — a durable record of a recurring intent, pinned to the
    # confirmed ICP / profile versions it targets (DCA-107). An ADMINISTRATOR scope: configuring the
    # agent's automation cadence is an administration action, so an ordinary acting user and a read-only
    # viewer both lack it — only a business administrator holds it. It is a business-plane scope, so a
    # platform operator (holding no membership) can never store a client's schedule with their global
    # rights alone. Storing a schedule runs nothing and sends nothing (the DCA has no send path);
    # advancing a due schedule and enqueuing its cycle stays a separate, disabled future capability.
    SCHEDULE_MANAGE = "schedule.manage"
    # set or clear a PER-BUSINESS emergency stop — a kill switch that pauses the DCA's background work
    # (claiming/advancing durable jobs, enqueuing due schedules) for ONE of this business's own agent
    # (DCA-111). An ADMINISTRATOR scope: pausing all of a business's agent work is a sensitive
    # operational decision, so an ordinary acting user and a read-only viewer both lack it — only a
    # business administrator holds it. It is a business-plane scope, so a platform operator (holding no
    # membership) can never set a client's business stop with their global rights alone; the
    # PLATFORM-WIDE stop (which dominates every business) is the separate platform scope below, never
    # this one. Setting a stop only ever makes the agent do LESS; it grants nothing and sends nothing
    # (the DCA has no send path — a disabled future capability regardless).
    EMERGENCY_STOP = "emergency.stop"

    # --- platform plane (granted to platform operators only) ---
    PLATFORM_OPS_READ = "platform.ops.read"  # operator: read platform-wide operational state
    # operator: approve a source configuration (DCA-046). A PLATFORM action, NOT a business one:
    # a source catalog entry and its terms are platform control-plane data shared across all tenants,
    # so approving them is never a tenant-admin right. Kept on the platform plane precisely so a
    # business administrator (``source.approve`` below) can never perform the catalog approval.
    SOURCE_TERMS_APPROVE = "platform.source.terms.approve"
    # operator: revoke a previously-approved source configuration (DCA-050). A distinct, explicit
    # platform scope from the approve scope above — revoking WITHDRAWS a right, a deliberately separate
    # authority from granting one — yet still never a tenant-admin right (the catalog is shared
    # control-plane data). A business administrator can never revoke a catalog approval.
    SOURCE_TERMS_REVOKE = "platform.source.terms.revoke"
    # operator: approve a compliance POLICY rulepack version (DCA-053). Like SOURCE_TERMS_APPROVE this
    # is a PLATFORM action, NOT a business one: a policy rulepack is platform control-plane data shared
    # across all tenants, so approving one is never a tenant-admin right. Kept on the platform plane
    # precisely so a business administrator (the ``policy.approve`` business scope above is a
    # business-local intent, not authority over the shared policy registry) can never perform it.
    POLICY_RULEPACK_APPROVE = "platform.policy.rulepack.approve"
    # operator: remove a catalogued source PLATFORM-WIDE (across every tenant) with a justification
    # (DCA-078). A PLATFORM action, NOT a business one: a source is shared control-plane data, so a
    # platform-wide removal is never a tenant-admin right. Kept on the platform plane precisely so a
    # business administrator can never perform it — a tenant may only place a LOCAL restriction
    # (the ``processing.restrict`` business scope above), never a platform-wide one.
    PLATFORM_SOURCE_RESTRICT = "platform.source.restrict"
    # operator: requeue a DEAD-LETTERED durable job for another attempt (DCA-105). A PLATFORM action on
    # the DCA's own background-work machinery — the durable job queue is operational infrastructure, so
    # recovering a parked job is a platform-operations decision, never a tenant-admin right. Kept on the
    # platform plane and distinct from the read scope above precisely so that reading platform ops
    # (``platform.ops.read``) never implies the authority to MUTATE a job's lifecycle, and a business
    # administrator (holding only business-plane scopes) can never requeue a job in any tenant.
    JOB_DEADLETTER_REQUEUE = "platform.job.deadletter.requeue"
    # operator: set or clear the PLATFORM-WIDE emergency stop — the top of the hierarchical kill switch
    # (DCA-111). A PLATFORM action on the DCA's OWN background-work machinery that DOMINATES every
    # business: while it is active no tenant's durable jobs are claimed/advanced and no due schedule is
    # enqueued, regardless of any per-business stop. The durable job queue is operational infrastructure,
    # so pausing it platform-wide is a platform-operations decision, never a tenant-admin right. Kept on
    # the platform plane and distinct from the per-business ``emergency.stop`` business scope precisely so
    # a business administrator (holding only business-plane scopes) can only pause its OWN agent and can
    # never pause the platform or another tenant. A stop grants nothing and sends nothing (the DCA has no
    # send path — a disabled future capability regardless).
    PLATFORM_EMERGENCY_STOP = "platform.emergency.stop"


# Business roles → the scopes they grant. Explicit and additive; an administrator is simply a role
# that is granted the sensitive scopes, NOT a bypass of the check. Unknown roles grant nothing.
_ADMIN_SCOPES = frozenset(
    {
        Scope.CANDIDATE_READ,
        Scope.FIELD_CORRECT,
        Scope.IDENTITY_MERGE,
        Scope.CONSENT_WITHDRAW,
        Scope.PROCESSING_RESTRICT,
        Scope.GRIEVANCE_READ,
        # erasing operational data is an administrator action — never an ordinary acting user's.
        Scope.DATA_ERASE,
        Scope.SOURCE_APPROVE,
        Scope.POLICY_APPROVE,
        Scope.BUDGET_MANAGE,
        # configuring the agent's recurring-schedule cadence is an administrator action (DCA-107).
        Scope.SCHEDULE_MANAGE,
        # pausing (or resuming) all of a business's own agent work is an administrator action (DCA-111).
        Scope.EMERGENCY_STOP,
    }
)
# The scopes an ordinary acting (non-admin, non-viewer) business user holds.
_ACTING_USER_SCOPES = frozenset(
    {
        Scope.CANDIDATE_READ,
        Scope.FIELD_CORRECT,
        Scope.IDENTITY_MERGE,
        Scope.CONSENT_WITHDRAW,
        Scope.PROCESSING_RESTRICT,
        Scope.GRIEVANCE_READ,
    }
)
BUSINESS_ROLE_SCOPES: dict[str, frozenset[Scope]] = {
    # read-only business user — may view suggestions but NOT edit/correct/restrict.
    "viewer": frozenset({Scope.CANDIDATE_READ}),
    # ordinary acting business users — may view, correct a field, withdraw a permission scope AND
    # place a local processing restriction about their own subjects (all normal acting-user actions).
    "user": _ACTING_USER_SCOPES,
    "member": _ACTING_USER_SCOPES,
    # business administrators
    "admin": _ADMIN_SCOPES,
    "owner": _ADMIN_SCOPES,
}

# The platform-operator role string and the platform scopes it grants. Kept separate from the
# business map on purpose: membership in a business can never yield a platform scope.
PLATFORM_OPERATOR_ROLE = "platform_operator"
PLATFORM_ROLE_SCOPES: dict[str, frozenset[Scope]] = {
    PLATFORM_OPERATOR_ROLE: frozenset(
        {
            Scope.PLATFORM_OPS_READ,
            Scope.SOURCE_TERMS_APPROVE,
            Scope.SOURCE_TERMS_REVOKE,
            Scope.POLICY_RULEPACK_APPROVE,
            Scope.PLATFORM_SOURCE_RESTRICT,
            Scope.JOB_DEADLETTER_REQUEUE,
            Scope.PLATFORM_EMERGENCY_STOP,
        }
    ),
}

_BUSINESS_SCOPES = frozenset().union(*BUSINESS_ROLE_SCOPES.values())
_PLATFORM_SCOPES = frozenset().union(*PLATFORM_ROLE_SCOPES.values())
assert _BUSINESS_SCOPES.isdisjoint(
    _PLATFORM_SCOPES
), "business and platform scopes must not overlap"


def scopes_for_business_roles(roles: tuple[str, ...]) -> frozenset[Scope]:
    """Business-plane scopes for a set of membership roles (union; unknown roles contribute none)."""
    granted: set[Scope] = set()
    for role in roles:
        granted |= BUSINESS_ROLE_SCOPES.get(role, frozenset())
    return frozenset(granted)


def scopes_for_platform_roles(roles: tuple[str, ...]) -> frozenset[Scope]:
    """Platform-plane scopes for a set of principal roles (union; unknown roles contribute none)."""
    granted: set[Scope] = set()
    for role in roles:
        granted |= PLATFORM_ROLE_SCOPES.get(role, frozenset())
    return frozenset(granted)


def is_platform_operator(roles: tuple[str, ...]) -> bool:
    """True if any role is a platform-operator role. Determined from the authenticated (signed)
    principal, never from a bare header."""
    return any(role in PLATFORM_ROLE_SCOPES for role in roles)
