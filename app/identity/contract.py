"""Typed identity/membership contract + the client Protocol (DCA-014).

Shapes only — no transport, no store. A request names a *subject* (an opaque platform user id) and
the *business* whose membership is being asked about. A response captures the five things the DCA
needs to make an authorization decision later: the subject, every business it may access, its roles
in the queried business, the membership expiry, and a version stamp for cache/concurrency.

Authority is always the platform's: per blocker B3 the DCA's ``X-Service-Auth`` authenticates the
caller service, NOT a tenant — so this contract is how per-business authority is (later) established.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class MembershipStatus(str, Enum):
    """A subject's standing in one business. Only ACTIVE grants roles / access."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    NONE = "none"  # no membership record linking this subject to this business


class IdentityRequest(BaseModel):
    """Resolve a subject's membership in exactly one business (DCA -> platform, B1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)  # opaque platform user id; never a credential
    business_id: str = Field(min_length=1)
    # Carried through so the (future) HTTP call and its logs share one id (DCA-012).
    correlation_id: str | None = None


class Membership(BaseModel):
    """A subject's standing in one business, as the platform reports it."""

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    status: MembershipStatus
    roles: tuple[str, ...] = ()
    expires_at: datetime | None = None


class IdentityResponse(BaseModel):
    """Resolved identity + membership for the queried business.

    Captures (story requirement) subject, allowed businesses, roles, expiry and version. ``roles`` is
    empty unless ``status`` is ACTIVE; ``allowed_businesses`` lists only businesses with an active
    membership, so a caller never treats an expired/revoked link as access.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    status: MembershipStatus
    roles: tuple[str, ...] = ()
    allowed_businesses: tuple[str, ...] = ()
    expires_at: datetime | None = None
    version: str = Field(min_length=1)

    @property
    def is_active(self) -> bool:
        return self.status is MembershipStatus.ACTIVE


@runtime_checkable
class IdentityClient(Protocol):
    """Port the DCA uses to resolve platform identity/membership.

    Synchronous, matching the existing service-to-service client style (``app/clients/ai_client.py``).
    The only implementation today is the dev/test fake; the live HTTP adapter is a later story and
    until it ships the contract is disabled in production.
    """

    def resolve(self, request: IdentityRequest) -> IdentityResponse: ...
