"""DCA-091 — evaluate Google Customer Match eligibility (the one composed audience-upload decision).

Key: ``google-policy`` · source sections 5, 10. Stage: India channel eligibility.

A pure, deterministic Python evaluator that answers ONE question for the Google Customer Match
channel — *may this customer-list audience be uploaded to Google Customer Match?* — by combining the
TWO concern-families Google's Customer Match policy keeps separate:

**A) data origin (first-party ONLY).** Google Customer Match accepts only data a business **collected
directly from its own customers** (first-party, with the customers' knowledge). Lists that were
**bought/purchased**, **discovered/scraped**, or otherwise obtained from a third party are prohibited
and may never be uploaded — regardless of how the business labels or "accepts" them. This gate is
evaluated **per member**: a single non-first-party contact blocks the whole audience.

**B) account / permission conditions for audience upload.** Uploading a Customer Match audience also
requires the advertising account itself to be eligible: the **Customer Match data policy must be
accepted**, the **account must be in good standing** (policy-compliant history), and the **terms must
be accepted**. These are account-level permissions, kept SEPARATE from the per-member data-origin gate.

**Origin is preserved through hashing.** Customer Match matches on **hashed** identifiers (SHA-256 over
a normalized email / E.164 phone) — the raw identifier never leaves the advertiser. But hashing is a
transform of the *identifier*, NOT of its *provenance*: :func:`hash_member` carries the member's
``origin`` through onto the resulting :class:`HashedMember`, unchanged. So a bought/discovered contact
is **still bought/discovered after it is hashed**, and the data-origin gate rejects it exactly the same.
Hashing can never launder a non-first-party origin into an uploadable one.

Load-bearing guarantees (the DCA-091 acceptance criteria):

- **Bought/discovered contacts are rejected even when hashed or "accepted" by the client.** The
  data-origin gate reads each member's preserved ``origin`` (which survives hashing untouched) and
  blocks the audience when any member is not first-party. A client self-attestation
  (``client_attested_first_party``) is RECORDED for audit but NEVER consulted by the decision — a
  client cannot vouch a purchased list into eligibility.
- **Reproducible, with the relevant denial, changing no state.** The decision is a pure function of its
  structured inputs at a given ``now`` — it reads nothing and writes nothing, so both the eligible
  result and every denial are perfectly reproducible with no state change.

This function is deliberately **not wired** into any upload, acquisition, worker or HTTP path — nothing
calls it except its own tests and the manual-check scenario. Concluding ELIGIBLE uploads nothing and
authorizes nothing: an actual Customer Match upload stays a disabled future capability (its own story,
with source-rights + rulepack + rights review). This is NOT recipient consent and NOT an upload
authorization.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from app.normalization.phones import STATUS_NORMALIZED, normalize_phone

# The one channel this evaluator speaks for — an audience-UPLOAD channel, not a messaging channel. (It
# is intentionally not a member of ``RuleChannel``, which enumerates outbound *message* channels.)
CUSTOMER_MATCH_CHANNEL = "google_customer_match"

# Customer Match matches on hashed identifiers; this is the one hashing scheme Google specifies.
HASH_ALGORITHM = "sha256"


class CustomerMatchOrigin(str, Enum):
    """How a Customer Match member entered the system — the data-origin gate's input.

    Only :attr:`FIRST_PARTY` is an uploadable origin. Every other origin is a non-first-party source
    Google's Customer Match policy prohibits, and is the acceptance invariant of this story:

    - ``FIRST_PARTY`` — collected directly from the data subject by the advertiser, with the subject's
      knowledge (a sign-up, a purchase, an account). The ONLY uploadable origin.
    - ``PURCHASED`` — a bought / rented contact list. Prohibited.
    - ``DISCOVERED`` — scraped / crawled / publicly listed / enriched from a third party. Prohibited.
    - ``PARTNER_PROVIDED`` — handed over by a third party / data partner (not the subject's own direct
      relationship with this advertiser). Prohibited.
    - ``UNKNOWN`` — unrecorded provenance: fail-closed (treated as non-first-party).
    """

    FIRST_PARTY = "first_party"
    PURCHASED = "purchased"
    DISCOVERED = "discovered"
    PARTNER_PROVIDED = "partner_provided"
    UNKNOWN = "unknown"


# The only origin that qualifies for a Customer Match upload. Everything else — including purchased and
# discovered contacts — is non-first-party and fails the data-origin gate.
FIRST_PARTY_ORIGINS = frozenset({CustomerMatchOrigin.FIRST_PARTY.value})


def origin_is_first_party(origin: str) -> bool:
    """Whether a member origin qualifies for upload (only a first-party origin does)."""
    return origin.strip().casefold() in FIRST_PARTY_ORIGINS


class MemberKeyType(str, Enum):
    """Which identifier a Customer Match member is keyed on. Both are normalized then SHA-256 hashed."""

    EMAIL = "email"
    PHONE = "phone"


class CustomerMatchEligibility(str, Enum):
    """The two outcomes of the composed Customer Match decision.

    - ``ELIGIBLE`` — both families passed: EVERY member is first-party AND the account conditions are
      met. The audience MAY be offered to a (future, disabled) upload path, which still re-gates
      everything. Not recipient consent, not an upload authorization.
    - ``BLOCKED`` — at least one gate withheld. Fail-closed is the default.
    """

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


# The two gate families, carried on every blocker so the separation is explicit and testable: the
# per-member DATA-ORIGIN family versus the ACCOUNT/permission conditions for upload.
FAMILY_DATA_ORIGIN = "data_origin"
FAMILY_ACCOUNT_PERMISSION = "account_permission"

# Stable, test-asserted reason codes — one per distinct gate that can withhold eligibility.
REASON_OK = "ok"  # ELIGIBLE: every gate passed
REASON_NON_FIRST_PARTY_MEMBERS = (
    "non_first_party_members"  # the audience contains bought/discovered (non-first-party) contacts
)
REASON_EMPTY_AUDIENCE = "empty_audience"  # no members to upload — nothing to be eligible
REASON_POLICY_NOT_ACCEPTED = (
    "customer_match_policy_not_accepted"  # the Customer Match data policy was not accepted
)
REASON_ACCOUNT_NOT_IN_GOOD_STANDING = (
    "account_not_in_good_standing"  # the account is not policy-compliant / eligible to upload
)
REASON_TERMS_NOT_ACCEPTED = "terms_not_accepted"  # the account terms were not accepted


def _normalize_email(raw: str) -> str:
    """Google's email normalization for hashing: strip surrounding whitespace, lowercase."""
    return raw.strip().casefold()


def _normalize_phone_for_hash(raw: str, *, country: str | None) -> str:
    """Google's phone normalization for hashing: E.164 when resolvable, else a fail-safe digit form.

    Prefers the service's :func:`normalize_phone` E.164 (the canonical Customer Match phone form). When
    the number stays unresolved it is NOT guessed into a country — the stripped/lowercased raw is hashed
    so the member is still representable; origin gating (the point of this story) is unaffected either way.
    """
    normalized = normalize_phone(raw, country=country)
    if normalized.status == STATUS_NORMALIZED and normalized.e164 is not None:
        return normalized.e164
    return raw.strip().casefold()


def _normalize_for_key(key_type: str, raw_value: str, *, country: str | None) -> str:
    kind = key_type.strip().casefold()
    if kind == MemberKeyType.EMAIL.value:
        return _normalize_email(raw_value)
    if kind == MemberKeyType.PHONE.value:
        return _normalize_phone_for_hash(raw_value, country=country)
    # Unknown key types are normalized conservatively (trim + lowercase) rather than rejected here —
    # this story gates on ORIGIN, not on the key scheme.
    return raw_value.strip().casefold()


@dataclass(frozen=True)
class HashedMember:
    """One Customer Match member, as it would be uploaded: a hashed identifier that PRESERVES its origin.

    ``hashed_identifier`` is the SHA-256 hex of the normalized identifier — the raw value is never
    stored. ``origin`` is the :class:`CustomerMatchOrigin` value, carried through the hashing transform
    UNCHANGED: that is the DCA-091 invariant — hashing changes the identifier, never the provenance, so
    the data-origin gate still sees (and rejects) a bought/discovered contact after it is hashed.
    """

    key_type: str
    hashed_identifier: str
    origin: str
    algorithm: str = HASH_ALGORITHM

    @property
    def origin_is_first_party(self) -> bool:
        return origin_is_first_party(self.origin)


def hash_member(
    *,
    key_type: str,
    raw_value: str,
    origin: str,
    country: str | None = None,
) -> HashedMember:
    """Normalize + SHA-256-hash one identifier into a :class:`HashedMember`, PRESERVING its origin.

    Pure and deterministic: the same ``(key_type, raw_value, origin, country)`` always yields the same
    hash and the same preserved origin. Crucially, ``origin`` is copied through verbatim — hashing the
    identifier can never strip or alter the provenance, so a non-first-party contact stays non-first-party.
    """
    normalized = _normalize_for_key(key_type, raw_value, country=country)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return HashedMember(
        key_type=key_type.strip().casefold(),
        hashed_identifier=digest,
        origin=origin,
        algorithm=HASH_ALGORITHM,
    )


@dataclass(frozen=True)
class CustomerMatchAccount:
    """The account / permission conditions for a Customer Match audience upload.

    All three must hold: the Customer Match data policy is accepted, the account is in good standing
    (policy-compliant, eligible to use Customer Match), and the terms are accepted. ``reason`` carries an
    upstream detail code for audit. Absent any one, the account-permission family blocks (fail-closed).
    """

    customer_match_policy_accepted: bool
    account_in_good_standing: bool
    terms_accepted: bool
    reason: str = ""

    @property
    def is_eligible(self) -> bool:
        return (
            self.customer_match_policy_accepted
            and self.account_in_good_standing
            and self.terms_accepted
        )


@dataclass(frozen=True)
class CustomerMatchEligibilityRequest:
    """The structured inputs to the composed Customer Match decision — one value object per family.

    ``members`` is the hashed audience (each :class:`HashedMember` carries its preserved origin).
    ``account`` is the account-permission family. ``client_attested_first_party`` is RECORDED CONTEXT
    ONLY — a client's self-assertion that "this list is fine" is carried for audit but NEVER consulted by
    the allow decision (a client cannot vouch a purchased list into eligibility). ``audience_name`` and
    ``purpose`` are provenance for the reported result.
    """

    audience_name: str
    purpose: str
    members: tuple[HashedMember, ...]
    account: CustomerMatchAccount
    client_attested_first_party: bool = False


@dataclass(frozen=True)
class RejectedMember:
    """A member the data-origin gate rejected — its key type, hashed id and (non-first-party) origin.

    Only the HASH is carried (never a raw identifier), proving the rejection survives hashing: the
    bought/discovered contact is identified solely by its hashed form plus its preserved origin.
    """

    key_type: str
    hashed_identifier: str
    origin: str


@dataclass(frozen=True)
class EligibilityBlocker:
    """One gate that withheld eligibility — its family, gate name, a stable reason code, and detail."""

    family: str
    gate: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class CustomerMatchEligibilityDecision:
    """The result of the composed Customer Match decision — reproducible and auditable, changing no state.

    ``blockers`` lists EVERY gate that withheld eligibility, in a fixed order (data-origin family first,
    then account-permission), each tagged with its :attr:`EligibilityBlocker.family`. ``rejected_members``
    names the non-first-party members (by hash) that failed the origin gate. ``decisive`` is the first
    blocker. ``eligible`` is true only when there are no blockers at all.
    """

    decision: CustomerMatchEligibility
    audience_name: str
    purpose: str
    channel: str
    member_count: int
    first_party_count: int
    rejected_members: tuple[RejectedMember, ...]
    blockers: tuple[EligibilityBlocker, ...]
    client_attested_first_party: bool
    evaluated_at: str

    @property
    def eligible(self) -> bool:
        return self.decision is CustomerMatchEligibility.ELIGIBLE

    @property
    def reasons(self) -> tuple[str, ...]:
        """Stable reason codes: ``(ok,)`` when eligible, else one per blocker in gate order."""
        if self.eligible:
            return (REASON_OK,)
        return tuple(b.reason for b in self.blockers)

    @property
    def decisive(self) -> EligibilityBlocker | None:
        return self.blockers[0] if self.blockers else None

    @property
    def data_origin_blockers(self) -> tuple[EligibilityBlocker, ...]:
        """The withheld gates in the per-member DATA-ORIGIN family (non-first-party / empty audience)."""
        return tuple(b for b in self.blockers if b.family == FAMILY_DATA_ORIGIN)

    @property
    def account_permission_blockers(self) -> tuple[EligibilityBlocker, ...]:
        """The withheld gates in the ACCOUNT-PERMISSION family (policy / good standing / terms)."""
        return tuple(b for b in self.blockers if b.family == FAMILY_ACCOUNT_PERMISSION)

    @property
    def data_origin_clear(self) -> bool:
        """Whether every member passed the data-origin gate (all first-party, audience non-empty)."""
        return not self.data_origin_blockers

    @property
    def account_conditions_met(self) -> bool:
        """Whether the account-permission family fully passed (policy + good standing + terms)."""
        return not self.account_permission_blockers

    @property
    def grants_consent(self) -> bool:
        """ELIGIBLE is never recipient consent, and never an upload authorization. Kept explicit so no
        caller mistakes it for one — the host owns consent and a future upload path owns the upload."""
        return False

    def permits_channel(self, channel: str) -> bool:
        """This decision authorises nothing beyond the Customer Match channel it evaluated. Only ``True``
        when ELIGIBLE AND ``channel`` is Customer Match — always ``False`` for email / SMS / WhatsApp."""
        return self.eligible and channel.strip().casefold() == self.channel.strip().casefold()


def decide_customer_match_eligibility(
    request: CustomerMatchEligibilityRequest, *, now: datetime | None = None
) -> CustomerMatchEligibilityDecision:
    """Combine the per-member data-origin family and the account-permission family into one Customer
    Match decision — pure, no I/O, no AI.

    Every gate is evaluated (not short-circuited) and each failure appends a blocker in a FIXED order,
    data-origin family first. ELIGIBLE requires ALL gates to pass. The data-origin gate reads each
    member's PRESERVED origin (which survives hashing untouched) and the ``client_attested_first_party``
    self-assertion is deliberately NOT consulted. The function reads nothing and changes nothing, so a
    given ``(request, now)`` always yields the identical result.
    """
    moment = now or datetime.now(UTC)
    members = request.members
    blockers: list[EligibilityBlocker] = []

    # -- A) DATA-ORIGIN family (first-party only — evaluated per member) -----------------------------
    #
    # Origin travels on each HashedMember, UNCHANGED by hashing, so a bought/discovered contact is still
    # caught here after it is hashed. The client's self-attestation is intentionally NOT read: a client
    # cannot vouch a purchased list into eligibility.
    rejected = tuple(
        RejectedMember(
            key_type=m.key_type,
            hashed_identifier=m.hashed_identifier,
            origin=m.origin,
        )
        for m in members
        if not m.origin_is_first_party
    )
    first_party_count = sum(1 for m in members if m.origin_is_first_party)

    if not members:
        blockers.append(
            EligibilityBlocker(
                FAMILY_DATA_ORIGIN,
                "audience",
                REASON_EMPTY_AUDIENCE,
                detail="0 members",
            )
        )
    elif rejected:
        origins = sorted({r.origin for r in rejected})
        blockers.append(
            EligibilityBlocker(
                FAMILY_DATA_ORIGIN,
                "member_origin",
                REASON_NON_FIRST_PARTY_MEMBERS,
                detail=f"{len(rejected)}/{len(members)} non-first-party; origins={origins}",
            )
        )

    # -- B) ACCOUNT-PERMISSION family (conditions for audience upload — SEPARATE from member origin) --
    account = request.account
    if not account.customer_match_policy_accepted:
        blockers.append(
            EligibilityBlocker(
                FAMILY_ACCOUNT_PERMISSION,
                "policy",
                REASON_POLICY_NOT_ACCEPTED,
                detail=account.reason,
            )
        )
    if not account.account_in_good_standing:
        blockers.append(
            EligibilityBlocker(
                FAMILY_ACCOUNT_PERMISSION,
                "good_standing",
                REASON_ACCOUNT_NOT_IN_GOOD_STANDING,
                detail=account.reason,
            )
        )
    if not account.terms_accepted:
        blockers.append(
            EligibilityBlocker(
                FAMILY_ACCOUNT_PERMISSION,
                "terms",
                REASON_TERMS_NOT_ACCEPTED,
                detail=account.reason,
            )
        )

    decision = CustomerMatchEligibility.BLOCKED if blockers else CustomerMatchEligibility.ELIGIBLE
    return CustomerMatchEligibilityDecision(
        decision=decision,
        audience_name=request.audience_name,
        purpose=request.purpose,
        channel=CUSTOMER_MATCH_CHANNEL,
        member_count=len(members),
        first_party_count=first_party_count,
        rejected_members=rejected,
        blockers=tuple(blockers),
        client_attested_first_party=request.client_attested_first_party,
        evaluated_at=moment.isoformat(),
    )


# -- convenience builders ----------------------------------------------------------------------------
# These hash raw members and assemble the request. They add no I/O and consult no model; they only
# normalize + hash + translate shapes, so the composition stays pure and reproducible.


def build_audience(
    raw_members: Iterable[tuple[str, str, str]],
    *,
    country: str | None = None,
) -> tuple[HashedMember, ...]:
    """Hash a sequence of ``(key_type, raw_value, origin)`` triples into :class:`HashedMember`\\ s.

    Each member is normalized + SHA-256 hashed with its origin preserved. Pure and deterministic.
    """
    return tuple(
        hash_member(key_type=key_type, raw_value=raw_value, origin=origin, country=country)
        for key_type, raw_value, origin in raw_members
    )


def evaluate_customer_match_eligibility(
    *,
    audience_name: str,
    purpose: str,
    raw_members: Iterable[tuple[str, str, str]],
    account: CustomerMatchAccount,
    client_attested_first_party: bool = False,
    country: str | None = None,
    now: datetime | None = None,
) -> CustomerMatchEligibilityDecision:
    """Hash the raw audience (preserving origin), assemble the request, then
    :func:`decide_customer_match_eligibility`.

    A convenience that takes raw ``(key_type, raw_value, origin)`` members, hashes them exactly as an
    upload would, and evaluates eligibility. Still pure: it opens no DB, calls no adapter and consults no
    model — demonstrating end-to-end that origin survives hashing and the gate rejects non-first-party
    contacts even in their hashed form.
    """
    members = build_audience(raw_members, country=country)
    request = CustomerMatchEligibilityRequest(
        audience_name=audience_name,
        purpose=purpose,
        members=members,
        account=account,
        client_attested_first_party=client_attested_first_party,
    )
    return decide_customer_match_eligibility(request, now=now)


def reason_codes(
    decisions: Iterable[CustomerMatchEligibilityDecision],
) -> list[tuple[str, ...]]:
    """Small helper for scenarios/tests: the reason tuples of a sequence of decisions, in order."""
    return [d.reasons for d in decisions]
