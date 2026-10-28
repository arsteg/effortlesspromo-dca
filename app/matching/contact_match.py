"""DCA-068 — match contacts (people) using STRONG, personal evidence, WITHOUT merging consent.

This is the person-level sibling of :mod:`app.matching.org_match` (DCA-067): a pure, deterministic,
network-free identity-resolution decision. It compares two :class:`ContactMatchSubject` bundles of
already-normalized contact evidence (a name, email/phone contact points, an employer, a role) and
returns a :class:`ContactMatchResult`. Like its sibling it is a *conclusion*, never an action: it
records nothing, mutates nothing, and NEVER merges two person observations or transfers one person's
permission/consent to another (``auto_merged`` and ``permission_transferred`` are ALWAYS ``False``).

Matching PEOPLE is deliberately more conservative than matching organizations, because a person
carries *permission* (consent, suppression, do-not-contact) that must never leak to the wrong human.
The decisive guarantee of this story:

- **A shared / recycled number or a common name NEVER transfers a person's permission to someone
  else.** A phone number can be reassigned by a carrier, shared across a household/desk, or printed on
  a company card; a name can be common. So a phone or a name is treated as WEAK, recyclable evidence:
  a weak overlap is at most a ``REVIEW`` candidate — surfaced for a human — and is NEVER promoted to a
  confirmed ``MATCH`` on its own. A shared *role / functional mailbox* (``info@``, ``sales@`` …) is
  likewise weak: many people answer it. When a weak overlap (a shared phone, a common name, a shared
  mailbox) is contradicted by DIFFERENT personal mailboxes on each side, the two are proven to be
  DIFFERENT people who merely share a recycled/shared contact point — ``NO_MATCH`` — so the permission
  of one never reaches the other.

- **Only a compatible PERSONAL contact point confirms a person.** Two subjects that share a personal
  email mailbox (a real per-person address, not a role/functional one) are the SAME person — the one
  strong anchor here. Even then, identity is confirmed, not consent: see reassignment below.

- **Reassignment / role changes require review, never a silent carry-over.** When a strong personal
  anchor agrees but the EMPLOYMENT (the matched employer organization) or the ROLE differs, the person
  has changed jobs/role: the result is ``REVIEW`` (a reassignment/role-change candidate), never an
  automatic ``MATCH`` that would silently carry the old employment context — and its permission —
  forward. A later, disabled capability may act on the review; this layer only flags it.

- **Location/employer corroborate or contradict; they never decide alone, and never create a match
  from weak evidence.** A shared employer raises a weak candidate's confidence (capped at ``MEDIUM``);
  a different employer lowers it. Employer agreement on its own, with no personal/weak anchor, is far
  too weak to even propose a candidate.

- **A denial changes nothing.** The function is pure, so both a positive result and the relevant
  denial — ``NO_MATCH`` on a recycled number, the refusal to collapse a common name/phone to anything
  stronger than ``REVIEW``, and a reassignment flagged for review — are perfectly reproducible with no
  state to change. Building a subject from raw strings (:func:`build_contact_subject`) runs them
  through the existing normalizers and COLLECTS any refusal as a :class:`ContactMatchDenial` rather
  than letting a malformed value silently masquerade as evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from app.db.models import ContactChannel
from app.matching.org_match import (
    Confidence,
    MatchOutcome,
    OrgMatchSubject,
    SignalRelation,
    SignalStrength,
    match_organizations,
    subject_from_observation,
)
from app.normalization.domains import normalize_domain
from app.normalization.errors import (
    REASON_EMPTY_EMAIL,
    REASON_INVALID_EMAIL,
    NormalizationError,
)
from app.normalization.names import NormalizedName, normalize_company_name
from app.normalization.phones import STATUS_NORMALIZED, normalize_phone

if TYPE_CHECKING:
    from app.db.models import PersonObservation
    from app.services.contact_point import ContactPointStore
    from app.services.org_observation import OrgObservationStore


class ContactMatchOutcome(str, Enum):
    """The categorical identity-resolution decision for a pair of person observations.

    - ``MATCH`` — a compatible PERSONAL contact point (a shared personal email mailbox) confirms the
      SAME person, with no conflicting employment/role. A conclusion only: it never merges the
      observations and never transfers permission.
    - ``NO_MATCH`` — a weak overlap (a shared/recycled phone, a common name, a shared role mailbox) is
      refuted by DIFFERENT personal mailboxes on each side: two different people sharing a recycled
      contact point. The permission of one never reaches the other.
    - ``REVIEW`` — a candidate to surface for a human, never an automatic match: either only WEAK
      evidence overlaps (a shared phone and/or a common name and/or a shared role mailbox), or a
      strong personal anchor agrees but EMPLOYMENT/ROLE differs (a reassignment/role change).
    - ``INSUFFICIENT`` — no match-worthy evidence overlaps (nothing, or only an employer signal with no
      personal/weak anchor), so not even a candidate is proposed.
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    REVIEW = "review"
    INSUFFICIENT = "insufficient"


class ContactSignalKind(str, Enum):
    """Which evidence dimension a :class:`ContactMatchSignal` came from.

    ``PERSONAL_EMAIL`` is the only STRONG personal anchor. ``SHARED_MAILBOX`` (a role/functional
    address), ``PHONE`` (recyclable/shareable) and ``NAME`` (can be common) are all WEAK.
    ``EMPLOYMENT`` / ``ROLE`` corroborate or contradict, and drive the reassignment/role-change review.
    """

    PERSONAL_EMAIL = "personal_email"
    SHARED_MAILBOX = "shared_mailbox"
    PHONE = "phone"
    NAME = "name"
    EMPLOYMENT = "employment"
    ROLE = "role"


@dataclass(frozen=True)
class ContactMatchSignal:
    """One piece of evidence that contributed to the decision, with its strength and relation.

    ``detail`` is an opaque, human-readable summary (a mailbox domain, a shared E.164, a role key). It
    deliberately carries NO raw personal contact value — never a full email local part or the person's
    display name — so the signal set stays safe to surface/audit.
    """

    kind: ContactSignalKind
    strength: SignalStrength
    relation: SignalRelation
    detail: str


@dataclass(frozen=True)
class ContactMatchDenial:
    """A raw input :func:`build_contact_subject` could not normalize/validate, captured, not raised.

    ``field`` names the input bucket (``name`` / ``email`` / ``phone``); ``code`` is the stable
    :class:`~app.normalization.errors.NormalizationError` code; ``detail`` echoes the offending input.
    Collecting (not raising) keeps a malformed value from silently becoming evidence while still
    letting the valid evidence be matched.
    """

    field: str
    code: str
    detail: str


# Functional / role mailbox local parts: addresses many people answer, so they are a SHARED mailbox,
# never a per-person anchor. Matching one across two subjects is a weak clustering hint only.
_ROLE_MAILBOX_LOCALPARTS = frozenset(
    {
        "info",
        "contact",
        "contactus",
        "sales",
        "support",
        "help",
        "helpdesk",
        "service",
        "services",
        "enquiry",
        "enquiries",
        "inquiry",
        "inquiries",
        "admin",
        "office",
        "hello",
        "team",
        "mail",
        "mailbox",
        "hr",
        "careers",
        "jobs",
        "accounts",
        "billing",
        "finance",
        "marketing",
        "feedback",
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "postmaster",
        "webmaster",
        "abuse",
    }
)

# The phone channels whose value is a (recyclable, shareable) dialable number.
_PHONE_CHANNELS = frozenset(
    {ContactChannel.MOBILE.value, ContactChannel.LANDLINE.value, ContactChannel.WHATSAPP.value}
)


@dataclass(frozen=True)
class _NormalizedEmail:
    address: str  # canonical ``local@host`` (local-part lower-cased, host = ascii domain)
    local: str
    host: str
    is_role: bool


def _normalize_email(raw: str) -> _NormalizedEmail:
    """Normalize a raw email into a canonical ``local@host`` form (pure; raises, mutates nothing).

    The local part is lower-cased and the host is canonicalized via the DCA-064 domain normalizer. A
    role/functional local part (``info``, ``sales`` …) is flagged so the matcher treats it as a SHARED
    mailbox, never a personal anchor. Refuses a blank value (``empty_email``) or a value that is not a
    single ``local@host`` address (``invalid_email``).
    """
    original = raw if raw is not None else ""
    trimmed = original.strip()
    if not trimmed:
        raise NormalizationError(REASON_EMPTY_EMAIL, "an email value is required", detail=original)
    if trimmed.count("@") != 1:
        raise NormalizationError(
            REASON_INVALID_EMAIL,
            "an email must be a single local@host address",
            detail=original,
        )
    local_raw, host_raw = trimmed.split("@", 1)
    local = local_raw.strip().lower()
    if not local:
        raise NormalizationError(
            REASON_INVALID_EMAIL, "an email must have a non-empty local part", detail=original
        )
    try:
        domain = normalize_domain(host_raw)
    except NormalizationError as exc:
        # Re-stamp as an email denial (the domain was only the host half of an address).
        raise NormalizationError(
            REASON_INVALID_EMAIL,
            f"'{host_raw}' is not a valid email host: {exc}",
            detail=original,
        ) from exc
    return _NormalizedEmail(
        address=f"{local}@{domain.ascii}",
        local=local,
        host=domain.ascii,
        is_role=local in _ROLE_MAILBOX_LOCALPARTS,
    )


@dataclass(frozen=True)
class ContactMatchSubject:
    """One person's already-normalized evidence bundle — the unit :func:`match_contacts` compares.

    Build one with :func:`build_contact_subject` (from raw strings) or
    :func:`subject_from_person_observation` (from a stored, tenant-scoped person observation). Nothing
    here is tenant state: a subject is a value object, and matching two subjects records, merges and
    transfers nothing. ``denials`` surfaces any raw input that failed to normalize when the subject was
    built.

    - ``personal_emails`` — canonical personal mailbox addresses (the only STRONG anchor).
    - ``shared_mailboxes`` — canonical role/functional addresses (WEAK — many people answer them).
    - ``phones`` — E.164 strings for successfully normalized phones (WEAK — recyclable/shareable).
    - ``employer`` — the person's employer as an :class:`~app.matching.org_match.OrgMatchSubject`
      (compared with the DCA-067 organization matcher), or ``None`` when unknown.
    - ``role_key`` — a casefolded role/title key (``""`` when unknown).
    """

    name: NormalizedName | None = None
    personal_emails: tuple[str, ...] = ()
    shared_mailboxes: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    employer: OrgMatchSubject | None = None
    role_key: str = ""
    denials: tuple[ContactMatchDenial, ...] = ()


# -- subject builders --------------------------------------------------------------------------------


def build_contact_subject(
    *,
    name: str | None = None,
    emails: Iterable[str] = (),
    phones: Iterable[str | tuple[str, str | None]] = (),
    employer: OrgMatchSubject | None = None,
    role_title: str | None = None,
) -> ContactMatchSubject:
    """Build a :class:`ContactMatchSubject` from RAW strings, running each through its normalizer.

    ``emails`` are raw addresses (each split into a personal vs role/functional mailbox). ``phones``
    are raw phone values, each either a bare string or a ``(value, country)`` pair (country defaults to
    ``"IN"`` — this is an India-only service); only a successfully normalized E.164 becomes a
    comparable key (an ambiguous/unresolved number is dropped, not a denial). ``employer`` is an
    optional pre-built organization subject. Any input that fails to normalize/validate is COLLECTED as
    a :class:`ContactMatchDenial` (never raised), so a malformed value cannot masquerade as evidence.
    Pure — mutates nothing.
    """
    denials: list[ContactMatchDenial] = []

    normalized_name: NormalizedName | None = None
    if name is not None and name.strip():
        try:
            normalized_name = normalize_company_name(name)
        except NormalizationError as exc:
            denials.append(
                ContactMatchDenial(field="name", code=exc.code, detail=exc.detail or name)
            )

    personal: list[str] = []
    shared: list[str] = []
    for raw in emails:
        try:
            email = _normalize_email(raw)
        except NormalizationError as exc:
            denials.append(
                ContactMatchDenial(field="email", code=exc.code, detail=exc.detail or raw)
            )
            continue
        (shared if email.is_role else personal).append(email.address)

    normalized_phones: list[str] = []
    for item in phones:
        if isinstance(item, tuple):
            raw_phone, country = item
        else:
            raw_phone, country = item, "IN"
        try:
            parsed = normalize_phone(raw_phone, country=country)
        except NormalizationError as exc:
            denials.append(
                ContactMatchDenial(field="phone", code=exc.code, detail=exc.detail or raw_phone)
            )
            continue
        # An UNRESOLVED (ambiguous) number is not a denial and not a comparable key — drop it.
        if parsed.status == STATUS_NORMALIZED and parsed.e164:
            normalized_phones.append(parsed.e164)

    return ContactMatchSubject(
        name=normalized_name,
        personal_emails=_unique(personal),
        shared_mailboxes=_unique(shared),
        phones=_unique(normalized_phones),
        employer=employer,
        role_key=_role_key(role_title),
        denials=tuple(denials),
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    """Deterministic de-duplication: sorted, unique, non-empty."""
    return tuple(sorted({v for v in values if v}))


def _role_key(role_title: str | None) -> str:
    """A casefolded, whitespace-collapsed role/title key (``""`` when unknown)."""
    if not role_title or not role_title.strip():
        return ""
    return " ".join(role_title.split()).casefold()


# -- the decision ------------------------------------------------------------------------------------


def _employment_signal(a: ContactMatchSubject, b: ContactMatchSubject) -> ContactMatchSignal | None:
    """Compare employers via the DCA-067 organization matcher. A different employer is a reassignment
    CONFLICT; the same employer corroborates; a weak/unknown comparison yields no signal."""
    if a.employer is None or b.employer is None:
        return None
    result = match_organizations(a.employer, b.employer)
    if result.outcome is MatchOutcome.MATCH:
        return ContactMatchSignal(
            ContactSignalKind.EMPLOYMENT,
            SignalStrength.WEAK,
            SignalRelation.CORROBORATE,
            "same employer organization",
        )
    if result.outcome is MatchOutcome.NO_MATCH:
        return ContactMatchSignal(
            ContactSignalKind.EMPLOYMENT,
            SignalStrength.WEAK,
            SignalRelation.CONFLICT,
            "different employer organization (reassignment)",
        )
    if result.outcome is MatchOutcome.REVIEW:
        return ContactMatchSignal(
            ContactSignalKind.EMPLOYMENT,
            SignalStrength.WEAK,
            SignalRelation.CORROBORATE,
            "likely same employer organization",
        )
    return None


def _role_signal(a: ContactMatchSubject, b: ContactMatchSubject) -> ContactMatchSignal | None:
    if not a.role_key or not b.role_key:
        return None
    if a.role_key == b.role_key:
        return ContactMatchSignal(
            ContactSignalKind.ROLE, SignalStrength.WEAK, SignalRelation.CORROBORATE, "same role"
        )
    return ContactMatchSignal(
        ContactSignalKind.ROLE, SignalStrength.WEAK, SignalRelation.CONFLICT, "different role"
    )


def _name_signal(a: ContactMatchSubject, b: ContactMatchSubject) -> ContactMatchSignal | None:
    if a.name is None or b.name is None:
        return None
    if a.name.match_key and a.name.match_key == b.name.match_key:
        return ContactMatchSignal(
            ContactSignalKind.NAME,
            SignalStrength.WEAK,
            SignalRelation.AGREE,
            "shared name match key",
        )
    return None


@dataclass(frozen=True)
class ContactMatchResult:
    """The identity-resolution decision for a pair of person subjects.

    ``outcome`` is the categorical decision; ``confidence`` grades the evidence; ``signals`` lists
    every evidence dimension that contributed. ``requires_reassignment_review`` is ``True`` when a
    strong personal anchor agrees but employment/role differs (a job/role change to review).

    ``auto_merged`` and ``permission_transferred`` are ALWAYS ``False``: a ``MATCH`` is a conclusion,
    never an automatic collapse of two observations, and this layer NEVER moves one person's
    permission/consent onto another (both stay disabled future capabilities).
    """

    outcome: ContactMatchOutcome
    confidence: Confidence
    signals: tuple[ContactMatchSignal, ...] = ()
    requires_reassignment_review: bool = False
    auto_merged: bool = False
    permission_transferred: bool = False

    def relation(self, kind: ContactSignalKind) -> SignalRelation | None:
        """The relation recorded for one evidence dimension, or ``None`` if it did not contribute."""
        for signal in self.signals:
            if signal.kind is kind:
                return signal.relation
        return None


def match_contacts(a: ContactMatchSubject, b: ContactMatchSubject) -> ContactMatchResult:
    """Decide whether two person subjects are the same, using strong PERSONAL evidence first.

    Deterministic and pure (mutates nothing, merges nothing, transfers no permission). Resolution
    order:

    1. A shared PERSONAL email mailbox AGREES (same per-person address) =>
       - if employment or role CONFLICTS => ``REVIEW`` (reassignment/role change — flagged, not
         carried over), ``MEDIUM``;
       - else => ``MATCH`` (``HIGH``).
    2. Otherwise, if only WEAK evidence overlaps (a shared/recycled phone, a common name, or a shared
       role mailbox):
       - if each side carries a DIFFERENT personal mailbox => ``NO_MATCH`` (``HIGH``): different people
         sharing a recycled/shared contact point — permission never transfers;
       - else => ``REVIEW`` candidate (``LOW``/``MEDIUM``), raised by employer corroboration and
         lowered by an employer/role conflict, but NEVER promoted to a match.
    3. Otherwise => ``INSUFFICIENT`` (``NONE``).
    """
    shared_personal = sorted(set(a.personal_emails) & set(b.personal_emails))
    both_have_personal = bool(a.personal_emails) and bool(b.personal_emails)
    personal_distinct = both_have_personal and not shared_personal

    shared_phones = sorted(set(a.phones) & set(b.phones))
    shared_mailboxes = sorted(set(a.shared_mailboxes) & set(b.shared_mailboxes))
    name_signal = _name_signal(a, b)
    employment_signal = _employment_signal(a, b)
    role_signal = _role_signal(a, b)

    weak_signals: list[ContactMatchSignal] = []
    if shared_phones:
        weak_signals.append(
            ContactMatchSignal(
                ContactSignalKind.PHONE,
                SignalStrength.WEAK,
                SignalRelation.AGREE,
                f"shared number (recyclable/shareable): {shared_phones[0]}",
            )
        )
    if shared_mailboxes:
        weak_signals.append(
            ContactMatchSignal(
                ContactSignalKind.SHARED_MAILBOX,
                SignalStrength.WEAK,
                SignalRelation.AGREE,
                f"shared role mailbox: {shared_mailboxes[0]}",
            )
        )
    if name_signal is not None:
        weak_signals.append(name_signal)

    # 1) Strong personal anchor: a shared per-person mailbox confirms the SAME person.
    if shared_personal:
        personal_signal = ContactMatchSignal(
            ContactSignalKind.PERSONAL_EMAIL,
            SignalStrength.STRONG,
            SignalRelation.AGREE,
            f"shared personal mailbox: {shared_personal[0].split('@', 1)[-1]}",
        )
        reassignment = _is_conflict(employment_signal) or _is_conflict(role_signal)
        signals = _collect([personal_signal, *weak_signals], employment_signal, role_signal)
        if reassignment:
            # Same person, but employer/role changed — flag the reassignment/role change for review
            # instead of silently carrying the prior employment context (and its permission) forward.
            return ContactMatchResult(
                ContactMatchOutcome.REVIEW,
                Confidence.MEDIUM,
                signals,
                requires_reassignment_review=True,
            )
        return ContactMatchResult(ContactMatchOutcome.MATCH, Confidence.HIGH, signals)

    # 2) Weak-only overlap: a shared/recycled number, a common name, or a shared role mailbox.
    if weak_signals:
        signals = _collect(weak_signals, employment_signal, role_signal)
        if personal_distinct:
            # The weak overlap (recycled/shared contact point or common name) is REFUTED by different
            # personal mailboxes: two different people. Never a match; permission never transfers.
            distinct_signal = ContactMatchSignal(
                ContactSignalKind.PERSONAL_EMAIL,
                SignalStrength.STRONG,
                SignalRelation.CONFLICT,
                "different personal mailboxes (recycled/shared contact point)",
            )
            return ContactMatchResult(
                ContactMatchOutcome.NO_MATCH, Confidence.HIGH, (*signals, distinct_signal)
            )
        confidence = Confidence.MEDIUM if len(weak_signals) >= 2 else Confidence.LOW
        if _is_conflict(employment_signal) or _is_conflict(role_signal):
            confidence = Confidence.LOW
        elif _is_corroborate(employment_signal) and confidence is Confidence.LOW:
            confidence = Confidence.MEDIUM
        return ContactMatchResult(ContactMatchOutcome.REVIEW, confidence, signals)

    # 3) Nothing match-worthy overlaps (a lone employer/role signal cannot propose a candidate).
    signals = _collect([], employment_signal, role_signal)
    return ContactMatchResult(ContactMatchOutcome.INSUFFICIENT, Confidence.NONE, signals)


def _is_conflict(signal: ContactMatchSignal | None) -> bool:
    return signal is not None and signal.relation is SignalRelation.CONFLICT


def _is_corroborate(signal: ContactMatchSignal | None) -> bool:
    return signal is not None and signal.relation is SignalRelation.CORROBORATE


def _collect(
    base: Sequence[ContactMatchSignal], *optional: ContactMatchSignal | None
) -> tuple[ContactMatchSignal, ...]:
    out: list[ContactMatchSignal] = list(base)
    out.extend(s for s in optional if s is not None)
    return tuple(out)


# -- read-only, tenant-scoped adapter over stored person observations -------------------------------


def subject_from_person_observation(
    contact_store: "ContactPointStore",
    person: "PersonObservation",
    *,
    org_store: "OrgObservationStore | None" = None,
) -> ContactMatchSubject:
    """Build a subject from ONE stored, tenant-scoped :class:`~app.db.models.PersonObservation`.

    ``contact_store`` is a :class:`~app.services.contact_point.ContactPointStore` already bound to the
    person's business, so every contact-point read stays within that tenant — matching is a
    WITHIN-business candidate-resolution aid and never reaches across tenants. ``org_store`` (also
    tenant-bound) is used, when supplied, to build the employer subject from the person's linked
    organization observation. Read-only: it stages/commits nothing, and merges nothing.

    Email contact points become personal vs role mailboxes; phone-channel contact points
    (mobile/landline/whatsapp) are normalized under the India plan into E.164 keys. The person's
    ``role_title`` becomes the role key.
    """
    emails: list[str] = []
    phones: list[str] = []
    for point in contact_store.contact_points_of(person.id):
        if point.channel == ContactChannel.EMAIL.value:
            emails.append(point.value)
        elif point.channel in _PHONE_CHANNELS:
            phones.append(point.value)

    employer: OrgMatchSubject | None = None
    if org_store is not None and person.org_observation_id:
        org = org_store.get(person.org_observation_id)
        if org is not None:
            employer = subject_from_observation(org_store, org)

    return build_contact_subject(
        name=person.display_name or None,
        emails=emails,
        phones=phones,
        employer=employer,
        role_title=person.role_title or None,
    )
