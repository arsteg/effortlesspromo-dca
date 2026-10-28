"""DCA-091 — evaluate Google Customer Match eligibility.

Pins the composed Customer Match audience-upload decision's acceptance behaviour:

- **Bought/discovered contacts are rejected even when hashed or "accepted" by the client.** Origin
  travels onto the :class:`HashedMember` unchanged by hashing, so the data-origin gate still catches a
  purchased/discovered contact in its hashed form. A client self-attestation is recorded but never
  consulted — it cannot vouch a non-first-party list into eligibility.
- **The account-permission family is kept distinct from member origin.** A fully first-party audience is
  still BLOCKED when the Customer Match policy / good standing / terms conditions are unmet.
- **Reproducible, changing no state.** The decision is a pure function of its structured inputs at a
  given ``now`` — nothing is read or written.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.compliance.customer_match_eligibility import (
    CUSTOMER_MATCH_CHANNEL,
    FAMILY_ACCOUNT_PERMISSION,
    FAMILY_DATA_ORIGIN,
    HASH_ALGORITHM,
    REASON_ACCOUNT_NOT_IN_GOOD_STANDING,
    REASON_EMPTY_AUDIENCE,
    REASON_NON_FIRST_PARTY_MEMBERS,
    REASON_OK,
    REASON_POLICY_NOT_ACCEPTED,
    REASON_TERMS_NOT_ACCEPTED,
    CustomerMatchAccount,
    CustomerMatchEligibility,
    CustomerMatchEligibilityRequest,
    CustomerMatchOrigin,
    MemberKeyType,
    build_audience,
    decide_customer_match_eligibility,
    evaluate_customer_match_eligibility,
    hash_member,
    origin_is_first_party,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
PURPOSE = "customer_match_audience_upload"
AUDIENCE = "loyal-customers-q4"


def _eligible_account() -> CustomerMatchAccount:
    return CustomerMatchAccount(
        customer_match_policy_accepted=True,
        account_in_good_standing=True,
        terms_accepted=True,
    )


def _first_party_member(email: str = "alice@example.com"):
    return hash_member(
        key_type=MemberKeyType.EMAIL.value,
        raw_value=email,
        origin=CustomerMatchOrigin.FIRST_PARTY.value,
    )


def _purchased_member(email: str = "bought@list.example"):
    return hash_member(
        key_type=MemberKeyType.EMAIL.value,
        raw_value=email,
        origin=CustomerMatchOrigin.PURCHASED.value,
    )


def _request(*, members, account=None, client_attested=False):
    return CustomerMatchEligibilityRequest(
        audience_name=AUDIENCE,
        purpose=PURPOSE,
        members=tuple(members),
        account=account or _eligible_account(),
        client_attested_first_party=client_attested,
    )


# -- the reproducible allow --------------------------------------------------------------------------


def test_all_first_party_with_eligible_account_is_eligible():
    result = decide_customer_match_eligibility(
        _request(members=[_first_party_member("a@x.com"), _first_party_member("b@x.com")]),
        now=NOW,
    )
    assert result.decision is CustomerMatchEligibility.ELIGIBLE
    assert result.eligible is True
    assert result.reasons == (REASON_OK,)
    assert result.blockers == ()
    assert result.channel == CUSTOMER_MATCH_CHANNEL
    assert result.member_count == 2
    assert result.first_party_count == 2
    assert result.rejected_members == ()
    assert result.data_origin_clear is True
    assert result.account_conditions_met is True
    assert result.grants_consent is False


# -- acceptance: bought/discovered contacts are rejected even when hashed ----------------------------


def test_purchased_contact_rejected_even_when_hashed():
    """The headline DCA-091 case: a bought contact, hashed exactly like a first-party one, is still
    rejected — origin survives hashing and the data-origin gate catches it."""
    result = decide_customer_match_eligibility(
        _request(
            members=[_first_party_member("ok@x.com"), _purchased_member("bought@list.example")]
        ),
        now=NOW,
    )
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.data_origin_clear is False
    assert result.reasons == (REASON_NON_FIRST_PARTY_MEMBERS,)
    assert result.decisive is not None
    assert result.decisive.family == FAMILY_DATA_ORIGIN
    # exactly the purchased member is named, identified ONLY by its hash (the raw value never surfaces)
    assert result.first_party_count == 1
    assert len(result.rejected_members) == 1
    rej = result.rejected_members[0]
    assert rej.origin == CustomerMatchOrigin.PURCHASED.value
    assert rej.hashed_identifier == hashlib.sha256(b"bought@list.example").hexdigest()


def test_discovered_contact_rejected_even_when_hashed():
    discovered = hash_member(
        key_type=MemberKeyType.PHONE.value,
        raw_value="+91 99000 00001",
        origin=CustomerMatchOrigin.DISCOVERED.value,
    )
    result = decide_customer_match_eligibility(_request(members=[discovered]), now=NOW)
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.reasons == (REASON_NON_FIRST_PARTY_MEMBERS,)
    assert result.rejected_members[0].origin == CustomerMatchOrigin.DISCOVERED.value


def test_client_attestation_cannot_vouch_a_purchased_list():
    """'accepted by the client' — a client's self-attestation is recorded but NEVER consulted."""
    result = decide_customer_match_eligibility(
        _request(members=[_purchased_member()], client_attested=True),
        now=NOW,
    )
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.client_attested_first_party is True  # recorded...
    assert result.reasons == (REASON_NON_FIRST_PARTY_MEMBERS,)  # ...but ignored by the decision


def test_hashing_preserves_origin_and_identifier_is_not_raw():
    """Hashing transforms the identifier but carries the origin through unchanged."""
    first = _first_party_member("Alice@Example.com")
    purchased = _purchased_member("Alice@Example.com")
    # Same raw identifier -> same hash (normalized: lowercased/trimmed), but origin is preserved distinctly.
    assert first.hashed_identifier == purchased.hashed_identifier
    assert first.hashed_identifier == hashlib.sha256(b"alice@example.com").hexdigest()
    assert first.hashed_identifier != "Alice@Example.com"
    assert first.algorithm == HASH_ALGORITHM
    assert first.origin == CustomerMatchOrigin.FIRST_PARTY.value
    assert purchased.origin == CustomerMatchOrigin.PURCHASED.value
    assert first.origin_is_first_party is True
    assert purchased.origin_is_first_party is False


def test_unknown_origin_fails_closed():
    unknown = hash_member(
        key_type=MemberKeyType.EMAIL.value,
        raw_value="who@x.com",
        origin=CustomerMatchOrigin.UNKNOWN.value,
    )
    result = decide_customer_match_eligibility(_request(members=[unknown]), now=NOW)
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.reasons == (REASON_NON_FIRST_PARTY_MEMBERS,)


def test_empty_audience_blocks():
    result = decide_customer_match_eligibility(_request(members=[]), now=NOW)
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.reasons == (REASON_EMPTY_AUDIENCE,)
    assert result.decisive.family == FAMILY_DATA_ORIGIN


# -- the account-permission family is real and separate ----------------------------------------------


def test_first_party_audience_blocked_when_policy_not_accepted():
    account = CustomerMatchAccount(
        customer_match_policy_accepted=False,
        account_in_good_standing=True,
        terms_accepted=True,
        reason="policy-not-signed",
    )
    result = decide_customer_match_eligibility(
        _request(members=[_first_party_member()], account=account), now=NOW
    )
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.data_origin_clear is True  # every member is first-party...
    assert (
        result.account_conditions_met is False
    )  # ...but the account condition independently denies
    assert result.reasons == (REASON_POLICY_NOT_ACCEPTED,)
    assert result.decisive.family == FAMILY_ACCOUNT_PERMISSION


def test_all_account_conditions_reported_in_order():
    account = CustomerMatchAccount(
        customer_match_policy_accepted=False,
        account_in_good_standing=False,
        terms_accepted=False,
    )
    result = decide_customer_match_eligibility(
        _request(members=[_first_party_member()], account=account), now=NOW
    )
    assert result.reasons == (
        REASON_POLICY_NOT_ACCEPTED,
        REASON_ACCOUNT_NOT_IN_GOOD_STANDING,
        REASON_TERMS_NOT_ACCEPTED,
    )
    assert all(b.family == FAMILY_ACCOUNT_PERMISSION for b in result.blockers)


def test_blockers_collected_in_fixed_family_order():
    """All gates evaluated (not short-circuited): data-origin family then account-permission family."""
    account = CustomerMatchAccount(
        customer_match_policy_accepted=False,
        account_in_good_standing=True,
        terms_accepted=True,
    )
    result = decide_customer_match_eligibility(
        _request(members=[_purchased_member()], account=account), now=NOW
    )
    families = [b.family for b in result.blockers]
    assert families == [FAMILY_DATA_ORIGIN, FAMILY_ACCOUNT_PERMISSION]
    assert result.reasons == (REASON_NON_FIRST_PARTY_MEMBERS, REASON_POLICY_NOT_ACCEPTED)


# -- scope / channel isolation -----------------------------------------------------------------------


def test_permits_only_customer_match_channel():
    result = decide_customer_match_eligibility(_request(members=[_first_party_member()]), now=NOW)
    assert result.permits_channel(CUSTOMER_MATCH_CHANNEL) is True
    assert result.permits_channel("email") is False
    assert result.permits_channel("whatsapp") is False


# -- convenience path, origin helper, purity ---------------------------------------------------------


def test_evaluate_convenience_all_first_party_eligible():
    result = evaluate_customer_match_eligibility(
        audience_name=AUDIENCE,
        purpose=PURPOSE,
        raw_members=[
            (MemberKeyType.EMAIL.value, "a@x.com", CustomerMatchOrigin.FIRST_PARTY.value),
            (MemberKeyType.PHONE.value, "+919900000001", CustomerMatchOrigin.FIRST_PARTY.value),
        ],
        account=_eligible_account(),
        now=NOW,
    )
    assert result.decision is CustomerMatchEligibility.ELIGIBLE
    assert result.member_count == 2


def test_evaluate_convenience_rejects_bought_member_after_hashing():
    result = evaluate_customer_match_eligibility(
        audience_name=AUDIENCE,
        purpose=PURPOSE,
        raw_members=[
            (MemberKeyType.EMAIL.value, "a@x.com", CustomerMatchOrigin.FIRST_PARTY.value),
            (MemberKeyType.EMAIL.value, "bought@list.example", CustomerMatchOrigin.PURCHASED.value),
        ],
        account=_eligible_account(),
        client_attested_first_party=True,
        now=NOW,
    )
    assert result.decision is CustomerMatchEligibility.BLOCKED
    assert result.reasons == (REASON_NON_FIRST_PARTY_MEMBERS,)
    # the uploaded members are hashed (no raw identifier retained on the rejection)
    assert all(len(r.hashed_identifier) == 64 for r in result.rejected_members)


def test_build_audience_hashes_and_preserves_origin():
    members = build_audience(
        [
            (MemberKeyType.EMAIL.value, " Bob@X.com ", CustomerMatchOrigin.FIRST_PARTY.value),
            (MemberKeyType.PHONE.value, "+91 99000 00001", CustomerMatchOrigin.PURCHASED.value),
        ]
    )
    assert members[0].hashed_identifier == hashlib.sha256(b"bob@x.com").hexdigest()
    assert members[0].origin == CustomerMatchOrigin.FIRST_PARTY.value
    assert members[1].hashed_identifier == hashlib.sha256(b"+919900000001").hexdigest()
    assert members[1].origin == CustomerMatchOrigin.PURCHASED.value


def test_origin_helper():
    assert origin_is_first_party(CustomerMatchOrigin.FIRST_PARTY.value) is True
    assert origin_is_first_party(CustomerMatchOrigin.PURCHASED.value) is False
    assert origin_is_first_party(CustomerMatchOrigin.DISCOVERED.value) is False
    assert origin_is_first_party(CustomerMatchOrigin.PARTNER_PROVIDED.value) is False
    assert origin_is_first_party(CustomerMatchOrigin.UNKNOWN.value) is False
    assert origin_is_first_party("  First_Party  ") is True  # tolerant of case/space


def test_decision_is_reproducible_and_changes_no_state():
    req = _request(members=[_first_party_member(), _purchased_member()])
    a = decide_customer_match_eligibility(req, now=NOW)
    b = decide_customer_match_eligibility(req, now=NOW)
    assert a == b
