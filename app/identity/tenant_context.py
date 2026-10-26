"""DCA-016 — the immutable, membership-verified tenant context.

Tenant authority is NEVER taken from a header or a token claim on its own. The requested
``business_id`` is resolved into a usable scope ONLY after the authenticated subject's *active*
membership in that business is confirmed through the identity contract (DCA-014). The result is a
frozen :class:`TenantContext`: once resolved it cannot be mutated, and an unresolved or denied scope
yields no context at all (fail-closed) — mirroring the .NET ``ITenantContext`` (resolve-once;
unresolved scope matches nothing).

The two halves of the seam compose here: DCA-015 established *who* the caller is
(:class:`~app.identity.tokens.AuthenticatedPrincipal`); this takes that subject plus a requested
business id and asks the platform (via the identity contract) *whether* that subject may act in that
business right now. Roles on the resulting context come from the membership record, never from the
bearer token's self-asserted claims — a token can never widen the tenant authority it is granted.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.identity.contract import IdentityClient, IdentityRequest, MembershipStatus
from app.identity.tokens import AuthenticatedPrincipal


class TenantResolutionError(Exception):
    """A requested business scope could not be resolved to an active membership.

    Carries whether a scope was supplied at all (so the boundary can distinguish a *missing* scope
    from a *denied* one) and a server-side-only reason. The HTTP layer collapses every denial to a
    single generic status/message so the response is never an oracle for whether the business exists,
    whether the subject has any link to it, or why a link is inactive.
    """

    def __init__(self, reason: str, *, scope_supplied: bool = True) -> None:
        super().__init__(reason)
        self.scope_supplied = scope_supplied


class TenantContext(BaseModel):
    """An immutable, membership-verified tenant scope. Frozen: resolved once, never mutated.

    ``roles`` are the platform's membership roles for this subject in this business (from the
    identity contract), NEVER the bearer token's claims. ``membership_version`` is the contract's
    version stamp, carried for later cache/concurrency decisions.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    roles: tuple[str, ...] = ()
    membership_version: str = Field(min_length=1)


def resolve_tenant_context(
    *,
    principal: AuthenticatedPrincipal,
    requested_business_id: str | None,
    identity_client: IdentityClient,
) -> TenantContext:
    """Resolve the authenticated subject + a requested business id into an immutable scope.

    Fail-closed at every step:

    - No business named → :class:`TenantResolutionError` (``scope_supplied=False``). We never guess a
      default tenant, so an unresolved scope grants nothing.
    - The subject's membership in the requested business is anything other than ACTIVE
      (NONE / EXPIRED / REVOKED) → denied. This is where an A-only subject asking for Business B lands.
    - Otherwise → a frozen :class:`TenantContext` carrying the membership's roles and version.
    """
    scope = (requested_business_id or "").strip()
    if not scope:
        raise TenantResolutionError("no business scope supplied", scope_supplied=False)

    response = identity_client.resolve(
        IdentityRequest(subject=principal.subject, business_id=scope)
    )
    # The ONLY thing that authorizes the scope is an ACTIVE membership of THIS subject in THIS
    # business. The ``allowed_businesses`` check is belt-and-braces: a resolver that ever reported
    # ACTIVE without listing the business would still be rejected here.
    if response.status is not MembershipStatus.ACTIVE or scope not in response.allowed_businesses:
        raise TenantResolutionError(
            f"subject has no active membership in the requested business "
            f"(status={response.status.value})"
        )

    return TenantContext(
        subject=principal.subject,
        business_id=response.business_id,
        roles=response.roles,
        membership_version=response.version,
    )
