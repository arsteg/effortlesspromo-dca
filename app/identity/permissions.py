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
    SOURCE_APPROVE = "source.approve"  # administrator: approve a data source
    POLICY_APPROVE = "policy.approve"  # administrator: approve a compliance policy
    BUDGET_MANAGE = "budget.manage"  # administrator: change spend budgets

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


# Business roles → the scopes they grant. Explicit and additive; an administrator is simply a role
# that is granted the sensitive scopes, NOT a bypass of the check. Unknown roles grant nothing.
_ADMIN_SCOPES = frozenset(
    {Scope.CANDIDATE_READ, Scope.SOURCE_APPROVE, Scope.POLICY_APPROVE, Scope.BUDGET_MANAGE}
)
BUSINESS_ROLE_SCOPES: dict[str, frozenset[Scope]] = {
    # ordinary business users
    "viewer": frozenset({Scope.CANDIDATE_READ}),
    "user": frozenset({Scope.CANDIDATE_READ}),
    "member": frozenset({Scope.CANDIDATE_READ}),
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
