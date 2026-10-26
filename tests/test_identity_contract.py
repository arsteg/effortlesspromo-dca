"""DCA-014 — the Python identity contract.

The deterministic fake distinguishes two users and tells active / expired / revoked / none apart;
responses capture subject, allowed businesses, roles, expiry and version; and production refuses the
fake (the contract stays disabled until the live adapter ships). Pure resolution — no state changes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.identity import (
    FakeIdentityClient,
    IdentityClient,
    IdentityContractDisabled,
    IdentityRequest,
    IdentityResponse,
    MembershipStatus,
    build_identity_client,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB
from tests.conftest import make_settings


def _resolve(client: IdentityClient, subject: str, business_id: str) -> IdentityResponse:
    return client.resolve(IdentityRequest(subject=subject, business_id=business_id))


def test_fake_is_an_identity_client():
    # The fake satisfies the runtime-checkable Protocol (the port other code will depend on).
    assert isinstance(FakeIdentityClient(), IdentityClient)


def test_fake_distinguishes_two_users():
    fake = FakeIdentityClient()
    alice = _resolve(fake, USER_ALICE, BUSINESS_A)
    bob = _resolve(fake, USER_BOB, BUSINESS_B)

    assert alice.status is MembershipStatus.ACTIVE
    assert bob.status is MembershipStatus.ACTIVE
    # Distinct subjects, roles, and access sets.
    assert alice.subject == USER_ALICE and bob.subject == USER_BOB
    assert alice.roles == ("owner", "campaign.admin")
    assert bob.roles == ("admin",)
    assert alice.allowed_businesses == (BUSINESS_A,)
    assert bob.allowed_businesses == (BUSINESS_B,)


def test_expired_membership_grants_nothing():
    expired = _resolve(FakeIdentityClient(), USER_ALICE, BUSINESS_B)
    assert expired.status is MembershipStatus.EXPIRED
    assert expired.is_active is False
    assert expired.roles == ()
    # B is not in Alice's allowed set even though a (lapsed) link exists.
    assert BUSINESS_B not in expired.allowed_businesses


def test_revoked_membership_grants_nothing():
    revoked = _resolve(FakeIdentityClient(), USER_BOB, BUSINESS_A)
    assert revoked.status is MembershipStatus.REVOKED
    assert revoked.roles == ()
    assert revoked.allowed_businesses == (BUSINESS_B,)  # only Bob's active business


def test_unknown_subject_or_business_is_none():
    fake = FakeIdentityClient()
    assert _resolve(fake, "nobody", BUSINESS_A).status is MembershipStatus.NONE
    assert _resolve(fake, USER_ALICE, "unknown-biz").status is MembershipStatus.NONE


def test_response_captures_all_required_fields():
    resp = _resolve(FakeIdentityClient(), USER_ALICE, BUSINESS_A)
    assert resp.subject == USER_ALICE
    assert resp.business_id == BUSINESS_A
    assert resp.allowed_businesses == (BUSINESS_A,)
    assert resp.roles == ("owner", "campaign.admin")
    assert resp.expires_at is not None
    assert resp.version == "fake-identity-v1"


def test_request_model_rejects_empty_and_unknown_fields():
    with pytest.raises(ValidationError):
        IdentityRequest(subject="", business_id=BUSINESS_A)
    with pytest.raises(ValidationError):
        IdentityRequest(subject=USER_ALICE, business_id="")
    with pytest.raises(ValidationError):
        IdentityRequest(subject=USER_ALICE, business_id=BUSINESS_A, role="admin")  # extra forbidden


def test_non_production_builds_the_fake():
    client = build_identity_client(make_settings(environment="development"))
    assert isinstance(client, FakeIdentityClient)


def test_production_refuses_the_fake():
    # Failure case: under production config the contract is disabled — no fake is handed back.
    with pytest.raises(IdentityContractDisabled):
        build_identity_client(make_settings(environment="production"))
