"""DCA-073 — expose separate quality dimensions as ONE typed, explainable quality summary.

This is a pure, deterministic, network-free *conclusion* layer — the sibling of the policy engine
(DCA-055) and the identity-resolution / canonical-selection conclusions (DCA-067/068/069). It takes
already-gathered, tenant-scoped evidence about ONE prospect subject and grades FIVE independent
quality dimensions — **identity confidence, freshness, completeness, deliverability and business
fit** — each with its own stable rating and reason code, so this one capability can be reviewed and
accepted on its own. It reads only what it is handed, computes value objects only, and writes /
mutates / deletes nothing: both a result and the relevant denial are perfectly reproducible with no
state to change. Acting on a summary (ranking, surfacing, acquiring) stays a disabled future
capability; this layer only grades and explains.

The load-bearing guarantees (the DCA-073 acceptance criteria):

- **Quality is kept STRICTLY separate from eligibility — a high business-fit score can NEVER change a
  blocked permission outcome.** Eligibility (the DCA-055 personal-data permission decision: may this
  record be acted on at all?) is a *different axis* from quality (how good is the data?). This
  summarizer NEVER computes or derives eligibility from any quality dimension; it carries the caller's
  pre-decided permission outcome through *verbatim* on a separate field. So raising ``business_fit`` to
  the top grade leaves a ``BLOCKED`` eligibility exactly ``BLOCKED`` — fit describes desirability, it
  grants nothing. A great prospect you are not permitted to contact stays not-permitted.

- **Catch-all deliverability remains UNCERTAIN, never "deliverable".** A catch-all / accept-all mail
  domain accepts any local-part at the SMTP layer, so acceptance proves nothing about the specific
  mailbox. Deliverability therefore grades a catch-all address ``UNCERTAIN`` (not ``DELIVERABLE``) even
  when the domain "accepts" it — the conservative, honest reading. Only an independently *verified*
  mailbox on a non-catch-all domain earns ``DELIVERABLE``.

Every dimension is graded INDEPENDENTLY from its own evidence; no dimension is allowed to raise or
lower another, and none touches eligibility. The vocabulary is deliberately small and stable (safe
codes, never free text) so a reviewer / later Web/Admin consumer can rely on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from app.compliance.policy_engine import PolicyDecision
from app.matching.org_match import Confidence, MatchOutcome

if TYPE_CHECKING:  # pragma: no cover - import only for type hints, no runtime dependency
    from app.db.models import FieldObservation


class QualityDimension(str, Enum):
    """The five independent quality dimensions a summary grades (DCA-073).

    Each is a SEPARATE axis of "how good is this record's data"; none is eligibility (see
    :class:`Eligibility`, carried apart). The set is fixed — a summary always reports exactly these.
    """

    IDENTITY_CONFIDENCE = "identity_confidence"
    FRESHNESS = "freshness"
    COMPLETENESS = "completeness"
    DELIVERABILITY = "deliverability"
    BUSINESS_FIT = "business_fit"


class QualityLevel(str, Enum):
    """The shared grade for the non-deliverability dimensions.

    ``HIGH`` / ``MEDIUM`` / ``LOW`` describe quality *strength*; ``UNKNOWN`` means there was no
    evidence to grade at all (never silently treated as good or bad). Deliverability uses its own
    :class:`Deliverability` vocabulary because "uncertain" is a first-class, required outcome there.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class Deliverability(str, Enum):
    """The deliverability grade — its own vocabulary because ``UNCERTAIN`` is load-bearing (DCA-073).

    - ``DELIVERABLE`` — an independently verified mailbox on a NON-catch-all domain.
    - ``UNDELIVERABLE`` — a hard-failure signal (invalid / bounced).
    - ``UNCERTAIN`` — present but unproven: a catch-all / accept-all domain (acceptance proves nothing),
      or an unverified address. Never upgraded to ``DELIVERABLE``.
    - ``UNKNOWN`` — no email contact point to assess at all.
    """

    DELIVERABLE = "deliverable"
    UNDELIVERABLE = "undeliverable"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"


class Eligibility(str, Enum):
    """The permission outcome carried ALONGSIDE quality — NOT a quality dimension (DCA-073).

    Mirrors the DCA-055 :class:`~app.compliance.policy_engine.PolicyDecision`: ``ALLOWED`` is the only
    value that grants the personal-data permission; ``BLOCKED`` / ``REVIEW`` / ``NOT_APPLICABLE`` grant
    nothing, and ``UNKNOWN`` means no decision was supplied (fail-safe: treated as not-permitted by any
    consumer). This summarizer copies the caller's decision through unchanged — it is never computed
    from a quality dimension, so no amount of quality can flip ``BLOCKED`` to ``ALLOWED``.
    """

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    REVIEW = "review"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


# -- stable reason codes (one per distinct path to a grade; safe codes, never free text) ------------

# identity confidence
REASON_IDENTITY_STRONG = (
    "identity_strong_evidence"  # a compatible/conflicting typed identifier decided
)
REASON_IDENTITY_CORROBORATED = "identity_corroborated"  # a match on softer-but-consistent evidence
REASON_IDENTITY_AMBIGUOUS = (
    "identity_ambiguous_review"  # only weak overlap: a human-review candidate
)
REASON_IDENTITY_UNRESOLVED = "identity_unresolved"  # no match-worthy evidence / not resolved

# freshness
REASON_FRESHNESS_FRESH = "freshness_fresh"
REASON_FRESHNESS_AGING = "freshness_aging"
REASON_FRESHNESS_STALE = "freshness_stale"
REASON_FRESHNESS_NO_OBSERVATIONS = "freshness_no_observations"

# completeness
REASON_COMPLETENESS_COMPLETE = "completeness_complete"
REASON_COMPLETENESS_PARTIAL = "completeness_partial"
REASON_COMPLETENESS_SPARSE = "completeness_sparse"
REASON_COMPLETENESS_NO_EXPECTED_FIELDS = "completeness_no_expected_fields"

# deliverability
REASON_DELIVERABILITY_VERIFIED = "deliverability_verified_mailbox"
REASON_DELIVERABILITY_CATCH_ALL = "deliverability_catch_all_uncertain"
REASON_DELIVERABILITY_UNVERIFIED = "deliverability_unverified_uncertain"
REASON_DELIVERABILITY_INVALID = "deliverability_invalid"
REASON_DELIVERABILITY_NO_CONTACT_POINT = "deliverability_no_contact_point"

# business fit
REASON_FIT_HIGH = "fit_high"
REASON_FIT_MEDIUM = "fit_medium"
REASON_FIT_LOW = "fit_low"
REASON_FIT_NO_SCORE = "fit_no_score"


# Default freshness thresholds (days). A record is only as fresh as its STALEST graded observation
# (the conservative reading): fresh < 90d, aging < 365d, stale >= 365d. Tunable per call.
DEFAULT_FRESH_MAX_DAYS = 90
DEFAULT_AGING_MAX_DAYS = 365

# Default completeness ratio thresholds (present-of-expected): complete >= 0.8, partial >= 0.4.
DEFAULT_COMPLETE_MIN_RATIO = 0.8
DEFAULT_PARTIAL_MIN_RATIO = 0.4

# Default business-fit score thresholds (0..1): high >= 0.75, medium >= 0.4, else low.
DEFAULT_FIT_HIGH_MIN = 0.75
DEFAULT_FIT_MEDIUM_MIN = 0.4


# -- typed evidence inputs --------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentitySignal:
    """Identity-resolution evidence for the subject (from DCA-067/068), already decided by the caller.

    ``outcome`` + ``confidence`` are the matcher's own conclusion for the record's identity; ``None``
    means no resolution was attempted (graded ``UNKNOWN``). This layer only maps that conclusion onto a
    quality grade — it never re-runs matching and never merges anything.
    """

    outcome: MatchOutcome | None = None
    confidence: Confidence | None = None


@dataclass(frozen=True)
class FreshnessSignal:
    """When the subject's graded field values were observed at the source (freshness evidence).

    ``observed_ats`` is the set of ``observed_at`` timestamps for the record's canonical/material
    fields. Empty means nothing was observed (graded ``UNKNOWN``). The grade uses the OLDEST timestamp
    — a record is only as fresh as its stalest graded field.
    """

    observed_ats: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class CompletenessSignal:
    """Which expected fields are actually present (completeness evidence).

    ``expected_fields`` is the set of field keys a record of this kind is expected to carry;
    ``present_fields`` is the set actually observed. An empty ``expected_fields`` is graded ``UNKNOWN``
    (there is no denominator to measure completeness against — never silently "complete").
    """

    expected_fields: frozenset[str] = frozenset()
    present_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DeliverabilitySignal:
    """One email contact point's deliverability evidence.

    ``verified`` — the specific mailbox was independently confirmed (e.g. a replied-to message / OTP).
    ``catch_all`` — the domain is catch-all / accept-all, so acceptance proves nothing about the
    mailbox. ``undeliverable`` — a hard failure (invalid / bounced). The grade is conservative:
    a catch-all is never ``DELIVERABLE`` (see module docstring).
    """

    verified: bool = False
    catch_all: bool = False
    undeliverable: bool = False


@dataclass(frozen=True)
class BusinessFitSignal:
    """The business-fit (desirability / ICP-match) evidence.

    ``score`` is a 0..1 match score (e.g. :attr:`~app.db.models.ProspectCandidate.match_score`);
    ``None`` is graded ``UNKNOWN``. ``band`` optionally overrides the score-to-grade mapping with a
    caller-supplied :class:`QualityLevel`. Business fit describes DESIRABILITY only — it grants no
    permission and is never consulted for eligibility.
    """

    score: float | None = None
    band: QualityLevel | None = None


@dataclass(frozen=True)
class EligibilityInput:
    """The caller's pre-decided permission outcome (DCA-055), carried through the summary UNCHANGED.

    This is NOT a quality dimension. ``decision`` is the DCA-055 :class:`PolicyDecision` (or its string
    value); ``reason`` is that decision's stable reason code. The summarizer copies both onto the
    summary's separate :class:`EligibilityOutcome` verbatim — it never derives eligibility from quality,
    so no quality grade can change it.
    """

    decision: PolicyDecision | str | None = None
    reason: str = ""


@dataclass(frozen=True)
class QualityEvidence:
    """Everything one quality summary is computed from — tenant-scoped, already gathered by the caller.

    Each field feeds exactly one dimension (plus the separate eligibility axis). Unsupplied signals are
    graded ``UNKNOWN`` rather than guessed.
    """

    identity: IdentitySignal = field(default_factory=IdentitySignal)
    freshness: FreshnessSignal = field(default_factory=FreshnessSignal)
    completeness: CompletenessSignal = field(default_factory=CompletenessSignal)
    deliverability: tuple[DeliverabilitySignal, ...] = ()
    business_fit: BusinessFitSignal = field(default_factory=BusinessFitSignal)
    eligibility: EligibilityInput = field(default_factory=EligibilityInput)


# -- typed outputs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionScore:
    """One graded quality dimension: its rating, the stable reason, and supporting detail."""

    dimension: QualityDimension
    rating: str
    reason: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension.value,
            "rating": self.rating,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class EligibilityOutcome:
    """The permission outcome, carried SEPARATELY from the quality dimensions (never derived from them)."""

    eligibility: Eligibility
    reason: str

    def to_dict(self) -> dict:
        return {"eligibility": self.eligibility.value, "reason": self.reason}


@dataclass(frozen=True)
class QualitySummary:
    """The typed, five-dimension quality summary with eligibility kept strictly separate (DCA-073)."""

    identity_confidence: DimensionScore
    freshness: DimensionScore
    completeness: DimensionScore
    deliverability: DimensionScore
    business_fit: DimensionScore
    # SEPARATE axis — a permission decision, never a quality grade and never derived from one.
    eligibility: EligibilityOutcome

    def dimensions(self) -> list[DimensionScore]:
        """The five quality dimensions in fixed order (eligibility is deliberately NOT included)."""
        return [
            self.identity_confidence,
            self.freshness,
            self.completeness,
            self.deliverability,
            self.business_fit,
        ]

    def to_dict(self) -> dict:
        return {
            "dimensions": {d.dimension.value: d.to_dict() for d in self.dimensions()},
            "eligibility": self.eligibility.to_dict(),
        }


# -- per-dimension grading (each independent; none reads another, none reads eligibility) -----------


def _grade_identity(signal: IdentitySignal) -> DimensionScore:
    outcome, confidence = signal.outcome, signal.confidence
    detail = {
        "outcome": outcome.value if outcome else None,
        "confidence": confidence.value if confidence else None,
    }
    if outcome in (MatchOutcome.MATCH, MatchOutcome.NO_MATCH) and confidence is Confidence.HIGH:
        # A compatible (MATCH) or conflicting (NO_MATCH) typed identifier decided it: unambiguous.
        return DimensionScore(
            QualityDimension.IDENTITY_CONFIDENCE,
            QualityLevel.HIGH.value,
            REASON_IDENTITY_STRONG,
            detail,
        )
    if outcome is MatchOutcome.MATCH:
        return DimensionScore(
            QualityDimension.IDENTITY_CONFIDENCE,
            QualityLevel.MEDIUM.value,
            REASON_IDENTITY_CORROBORATED,
            detail,
        )
    if outcome is MatchOutcome.REVIEW:
        # Only weak evidence overlapped — an ambiguous identity to review, never a confident one.
        return DimensionScore(
            QualityDimension.IDENTITY_CONFIDENCE,
            QualityLevel.LOW.value,
            REASON_IDENTITY_AMBIGUOUS,
            detail,
        )
    # INSUFFICIENT or no resolution attempted.
    return DimensionScore(
        QualityDimension.IDENTITY_CONFIDENCE,
        QualityLevel.UNKNOWN.value,
        REASON_IDENTITY_UNRESOLVED,
        detail,
    )


def _grade_freshness(
    signal: FreshnessSignal,
    *,
    now: datetime,
    fresh_max_days: int,
    aging_max_days: int,
) -> DimensionScore:
    observed = [_as_utc(ts) for ts in signal.observed_ats if ts is not None]
    if not observed:
        return DimensionScore(
            QualityDimension.FRESHNESS,
            QualityLevel.UNKNOWN.value,
            REASON_FRESHNESS_NO_OBSERVATIONS,
            {"observation_count": 0},
        )
    oldest = min(observed)  # a record is only as fresh as its stalest graded field
    age_days = (now - oldest).total_seconds() / 86400.0
    detail = {
        "observation_count": len(observed),
        "oldest_observed_at": oldest.isoformat(),
        "oldest_age_days": round(age_days, 2),
    }
    if age_days < fresh_max_days:
        return DimensionScore(
            QualityDimension.FRESHNESS, QualityLevel.HIGH.value, REASON_FRESHNESS_FRESH, detail
        )
    if age_days < aging_max_days:
        return DimensionScore(
            QualityDimension.FRESHNESS, QualityLevel.MEDIUM.value, REASON_FRESHNESS_AGING, detail
        )
    return DimensionScore(
        QualityDimension.FRESHNESS, QualityLevel.LOW.value, REASON_FRESHNESS_STALE, detail
    )


def _grade_completeness(
    signal: CompletenessSignal,
    *,
    complete_min_ratio: float,
    partial_min_ratio: float,
) -> DimensionScore:
    expected = frozenset(signal.expected_fields)
    if not expected:
        return DimensionScore(
            QualityDimension.COMPLETENESS,
            QualityLevel.UNKNOWN.value,
            REASON_COMPLETENESS_NO_EXPECTED_FIELDS,
            {"expected_count": 0},
        )
    present = frozenset(signal.present_fields) & expected
    missing = sorted(expected - present)
    ratio = len(present) / len(expected)
    detail = {
        "expected_count": len(expected),
        "present_count": len(present),
        "ratio": round(ratio, 4),
        "missing_fields": missing,
    }
    if ratio >= complete_min_ratio:
        return DimensionScore(
            QualityDimension.COMPLETENESS,
            QualityLevel.HIGH.value,
            REASON_COMPLETENESS_COMPLETE,
            detail,
        )
    if ratio >= partial_min_ratio:
        return DimensionScore(
            QualityDimension.COMPLETENESS,
            QualityLevel.MEDIUM.value,
            REASON_COMPLETENESS_PARTIAL,
            detail,
        )
    return DimensionScore(
        QualityDimension.COMPLETENESS, QualityLevel.LOW.value, REASON_COMPLETENESS_SPARSE, detail
    )


def _grade_deliverability(signals: Sequence[DeliverabilitySignal]) -> DimensionScore:
    if not signals:
        return DimensionScore(
            QualityDimension.DELIVERABILITY,
            Deliverability.UNKNOWN.value,
            REASON_DELIVERABILITY_NO_CONTACT_POINT,
            {"contact_point_count": 0},
        )
    detail = {
        "contact_point_count": len(signals),
        "any_catch_all": any(s.catch_all for s in signals),
        "any_verified": any(s.verified for s in signals),
        "any_undeliverable": any(s.undeliverable for s in signals),
    }
    # Worst / most-conservative wins. A hard failure dominates.
    if any(s.undeliverable for s in signals):
        return DimensionScore(
            QualityDimension.DELIVERABILITY,
            Deliverability.UNDELIVERABLE.value,
            REASON_DELIVERABILITY_INVALID,
            detail,
        )
    # A verified mailbox on a NON-catch-all domain is the only path to DELIVERABLE.
    if any(s.verified and not s.catch_all for s in signals):
        return DimensionScore(
            QualityDimension.DELIVERABILITY,
            Deliverability.DELIVERABLE.value,
            REASON_DELIVERABILITY_VERIFIED,
            detail,
        )
    # A catch-all domain accepts anything, so it can never be upgraded past UNCERTAIN — even if the
    # domain "verified"/accepted the address. This is the load-bearing DCA-073 behaviour.
    if any(s.catch_all for s in signals):
        return DimensionScore(
            QualityDimension.DELIVERABILITY,
            Deliverability.UNCERTAIN.value,
            REASON_DELIVERABILITY_CATCH_ALL,
            detail,
        )
    # Present but unverified and not catch-all: still uncertain, honestly.
    return DimensionScore(
        QualityDimension.DELIVERABILITY,
        Deliverability.UNCERTAIN.value,
        REASON_DELIVERABILITY_UNVERIFIED,
        detail,
    )


def _grade_business_fit(
    signal: BusinessFitSignal,
    *,
    fit_high_min: float,
    fit_medium_min: float,
) -> DimensionScore:
    if signal.band is not None:
        reason = {
            QualityLevel.HIGH: REASON_FIT_HIGH,
            QualityLevel.MEDIUM: REASON_FIT_MEDIUM,
            QualityLevel.LOW: REASON_FIT_LOW,
            QualityLevel.UNKNOWN: REASON_FIT_NO_SCORE,
        }[signal.band]
        return DimensionScore(
            QualityDimension.BUSINESS_FIT,
            signal.band.value,
            reason,
            {"band": signal.band.value, "score": signal.score},
        )
    if signal.score is None:
        return DimensionScore(
            QualityDimension.BUSINESS_FIT,
            QualityLevel.UNKNOWN.value,
            REASON_FIT_NO_SCORE,
            {"score": None},
        )
    detail = {"score": signal.score}
    if signal.score >= fit_high_min:
        return DimensionScore(
            QualityDimension.BUSINESS_FIT, QualityLevel.HIGH.value, REASON_FIT_HIGH, detail
        )
    if signal.score >= fit_medium_min:
        return DimensionScore(
            QualityDimension.BUSINESS_FIT, QualityLevel.MEDIUM.value, REASON_FIT_MEDIUM, detail
        )
    return DimensionScore(
        QualityDimension.BUSINESS_FIT, QualityLevel.LOW.value, REASON_FIT_LOW, detail
    )


# Map a DCA-055 PolicyDecision onto the separate eligibility axis. ``DENY`` surfaces as ``BLOCKED``.
_DECISION_TO_ELIGIBILITY: dict[PolicyDecision, Eligibility] = {
    PolicyDecision.ALLOW: Eligibility.ALLOWED,
    PolicyDecision.DENY: Eligibility.BLOCKED,
    PolicyDecision.REVIEW: Eligibility.REVIEW,
    PolicyDecision.NOT_APPLICABLE: Eligibility.NOT_APPLICABLE,
}


def _carry_eligibility(input_: EligibilityInput) -> EligibilityOutcome:
    """Carry the caller's permission decision through UNCHANGED (never derived from any quality grade)."""
    decision = input_.decision
    if isinstance(decision, PolicyDecision):
        eligibility = _DECISION_TO_ELIGIBILITY[decision]
    elif isinstance(decision, str) and decision:
        try:
            eligibility = _DECISION_TO_ELIGIBILITY[PolicyDecision(decision)]
        except ValueError:
            eligibility = Eligibility.UNKNOWN
    else:
        eligibility = Eligibility.UNKNOWN
    return EligibilityOutcome(eligibility=eligibility, reason=input_.reason)


def summarize_quality(
    evidence: QualityEvidence,
    *,
    now: datetime | None = None,
    fresh_max_days: int = DEFAULT_FRESH_MAX_DAYS,
    aging_max_days: int = DEFAULT_AGING_MAX_DAYS,
    complete_min_ratio: float = DEFAULT_COMPLETE_MIN_RATIO,
    partial_min_ratio: float = DEFAULT_PARTIAL_MIN_RATIO,
    fit_high_min: float = DEFAULT_FIT_HIGH_MIN,
    fit_medium_min: float = DEFAULT_FIT_MEDIUM_MIN,
) -> QualitySummary:
    """Grade the five quality dimensions independently and carry eligibility through separately (pure).

    Every dimension is computed ONLY from its own evidence; no dimension reads another, and NONE reads
    eligibility. Eligibility is the caller's pre-decided DCA-055 permission outcome, copied through
    verbatim — so a top ``business_fit`` grade can never change a ``BLOCKED`` eligibility. A pure
    function of its inputs at ``now``: it reads only what it is handed, mutates / records nothing, and
    returns the identical result on a repeat call.
    """
    moment = _as_utc(now) if now is not None else datetime.now(UTC)
    return QualitySummary(
        identity_confidence=_grade_identity(evidence.identity),
        freshness=_grade_freshness(
            evidence.freshness,
            now=moment,
            fresh_max_days=fresh_max_days,
            aging_max_days=aging_max_days,
        ),
        completeness=_grade_completeness(
            evidence.completeness,
            complete_min_ratio=complete_min_ratio,
            partial_min_ratio=partial_min_ratio,
        ),
        deliverability=_grade_deliverability(evidence.deliverability),
        business_fit=_grade_business_fit(
            evidence.business_fit, fit_high_min=fit_high_min, fit_medium_min=fit_medium_min
        ),
        # SEPARATE axis — carried through unchanged, never derived from a quality grade above.
        eligibility=_carry_eligibility(evidence.eligibility),
    )


# -- read-only adapters over stored field observations (grounding, no new scope, no writes) ---------


def freshness_from_observations(observations: Iterable["FieldObservation"]) -> FreshnessSignal:
    """Build a :class:`FreshnessSignal` from stored DCA-061 field observations (read-only).

    The caller supplies observations already read within its tenant scope (e.g.
    ``FieldObservationStore.for_subject(...)``); this reads only their ``observed_at`` and widens no
    scope. Builds a value object only.
    """
    return FreshnessSignal(observed_ats=tuple(obs.observed_at for obs in observations))


def completeness_from_observations(
    observations: Iterable["FieldObservation"], *, expected_fields: Iterable[str]
) -> CompletenessSignal:
    """Build a :class:`CompletenessSignal` from stored observations + the expected field set (read-only).

    ``present_fields`` is the set of distinct ``field_key``s actually observed (only those carrying a
    non-empty value count as present). Reads only what it is handed; builds a value object only.
    """
    present = {obs.field_key for obs in observations if (obs.field_value or "").strip()}
    return CompletenessSignal(
        expected_fields=frozenset(expected_fields), present_fields=frozenset(present)
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
