"""Deterministic, in-memory identity fake for local tests (DCA-014).

The fake is the ONLY identity implementation today: the live HTTP adapter to the platform (B1) is a
later story. It is dev/test-only — production refuses it (``build_identity_client``), mirroring the
seed's dev-only stance (``app/db/seed.py``). Everything here is synthetic and obviously fake; no real
user, credential, or contact is involved.

Standing is derived with real logic (not hardcoded strings): an explicitly revoked link is REVOKED;
otherwise an ``expires_at`` in the past is EXPIRED; otherwise ACTIVE. Fixture dates are far-past /
far-future so the outcome is stable regardless of the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.identity.contract import (
    IdentityRequest,
    IdentityResponse,
    MembershipStatus,
)

# Synthetic businesses — the same two the dev seed uses (A and B), so fixtures line up.
BUSINESS_A = "00000000-0000-4000-8000-00000000000a"
BUSINESS_B = "00000000-0000-4000-8000-00000000000b"

# Synthetic subjects (opaque platform user ids). Deterministic, obviously fake.
USER_ALICE = "00000000-0000-4000-9000-0000000a11ce"
USER_BOB = "00000000-0000-4000-9000-00000000b0b0"

_PAST = datetime(2020, 1, 1, tzinfo=UTC)
_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)

_FAKE_VERSION = "fake-identity-v1"


class IdentityContractDisabled(RuntimeError):
    """Raised when identity resolution is requested where the contract is not wired (production)."""


@dataclass(frozen=True)
class _Link:
    """A raw fixture row: a subject's link to a business, before status is derived."""

    roles: tuple[str, ...]
    revoked: bool = False
    expires_at: datetime | None = None


# Two distinct users with distinct, deterministic standings covering active / expired / revoked:
#   Alice — active owner of A; her B link has lapsed (expired).
#   Bob   — active admin of B; his A link was revoked.
_DEFAULT_LINKS: dict[str, dict[str, _Link]] = {
    USER_ALICE: {
        BUSINESS_A: _Link(roles=("owner", "campaign.admin"), expires_at=_FUTURE),
        BUSINESS_B: _Link(roles=("viewer",), expires_at=_PAST),
    },
    USER_BOB: {
        BUSINESS_B: _Link(roles=("admin",), expires_at=_FUTURE),
        BUSINESS_A: _Link(roles=("owner",), revoked=True),
    },
}


def _derive_status(link: _Link, now: datetime) -> MembershipStatus:
    if link.revoked:
        return MembershipStatus.REVOKED
    if link.expires_at is not None and link.expires_at <= now:
        return MembershipStatus.EXPIRED
    return MembershipStatus.ACTIVE


class FakeIdentityClient:
    """A deterministic :class:`~app.identity.contract.IdentityClient`. Pure: no I/O, no state change.

    ``now`` is injectable so expiry is testable without a wall clock; it defaults to the real UTC now
    (the fixture dates are far-past/future, so the default is still deterministic in practice).
    """

    def __init__(
        self,
        links: dict[str, dict[str, _Link]] | None = None,
        *,
        now: datetime | None = None,
        version: str = _FAKE_VERSION,
    ) -> None:
        self._links = links if links is not None else _DEFAULT_LINKS
        self._now = now
        self._version = version

    def resolve(self, request: IdentityRequest) -> IdentityResponse:
        now = self._now or datetime.now(UTC)
        subject_links = self._links.get(request.subject, {})

        link = subject_links.get(request.business_id)
        status = _derive_status(link, now) if link is not None else MembershipStatus.NONE
        # Roles and expiry are only meaningful when the membership is currently active.
        roles = link.roles if (link is not None and status is MembershipStatus.ACTIVE) else ()
        expires_at = link.expires_at if link is not None else None

        allowed = tuple(
            sorted(
                business_id
                for business_id, other in subject_links.items()
                if _derive_status(other, now) is MembershipStatus.ACTIVE
            )
        )

        return IdentityResponse(
            subject=request.subject,
            business_id=request.business_id,
            status=status,
            roles=roles,
            allowed_businesses=allowed,
            expires_at=expires_at,
            version=self._version,
        )


def build_identity_client(settings: Any) -> FakeIdentityClient:
    """The identity client for this deployment.

    Only the dev/test fake exists; the live adapter (B1) is unbuilt. Production therefore has NO
    identity client — the contract stays disabled until that story ships. Calling this under
    production configuration raises rather than silently handing back a fake.
    """
    if getattr(settings, "is_production", False):
        raise IdentityContractDisabled(
            "identity contract is disabled in production: the live adapter is not built yet and the "
            "deterministic fake is dev/test-only (B1)"
        )
    return FakeIdentityClient()
