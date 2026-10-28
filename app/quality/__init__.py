"""Separate, explainable quality dimensions for a prospect record (DCA-073).

A pure, deterministic, network-free *conclusion* layer (the sibling of :mod:`app.matching` and
:mod:`app.canonical`). It grades FIVE independent quality dimensions — identity confidence, freshness,
completeness, deliverability and business fit — each with its own stable rating + reason, and carries
the DCA-055 permission decision through on a SEPARATE eligibility axis. A high business-fit grade can
never change a blocked eligibility, and a catch-all mail domain is graded ``UNCERTAIN`` (never
``DELIVERABLE``). It reads only what it is handed and writes / mutates / deletes nothing; acting on a
summary stays a disabled future capability.

Public surface:

- :func:`summarize_quality` — the pure summarizer.
- :class:`QualitySummary` / :class:`DimensionScore` / :class:`EligibilityOutcome`.
- :class:`QualityEvidence` and its typed signals (:class:`IdentitySignal`, :class:`FreshnessSignal`,
  :class:`CompletenessSignal`, :class:`DeliverabilitySignal`, :class:`BusinessFitSignal`,
  :class:`EligibilityInput`).
- The rating vocabularies (:class:`QualityDimension`, :class:`QualityLevel`, :class:`Deliverability`,
  :class:`Eligibility`) and the stable ``REASON_*`` codes.
- :func:`freshness_from_observations` / :func:`completeness_from_observations` — read-only adapters
  over stored DCA-061 field observations.
"""

from __future__ import annotations

from app.quality.quality_summary import (
    DEFAULT_AGING_MAX_DAYS,
    DEFAULT_COMPLETE_MIN_RATIO,
    DEFAULT_FIT_HIGH_MIN,
    DEFAULT_FIT_MEDIUM_MIN,
    DEFAULT_FRESH_MAX_DAYS,
    DEFAULT_PARTIAL_MIN_RATIO,
    REASON_COMPLETENESS_COMPLETE,
    REASON_COMPLETENESS_NO_EXPECTED_FIELDS,
    REASON_COMPLETENESS_PARTIAL,
    REASON_COMPLETENESS_SPARSE,
    REASON_DELIVERABILITY_CATCH_ALL,
    REASON_DELIVERABILITY_INVALID,
    REASON_DELIVERABILITY_NO_CONTACT_POINT,
    REASON_DELIVERABILITY_UNVERIFIED,
    REASON_DELIVERABILITY_VERIFIED,
    REASON_FIT_HIGH,
    REASON_FIT_LOW,
    REASON_FIT_MEDIUM,
    REASON_FIT_NO_SCORE,
    REASON_FRESHNESS_AGING,
    REASON_FRESHNESS_FRESH,
    REASON_FRESHNESS_NO_OBSERVATIONS,
    REASON_FRESHNESS_STALE,
    REASON_IDENTITY_AMBIGUOUS,
    REASON_IDENTITY_CORROBORATED,
    REASON_IDENTITY_STRONG,
    REASON_IDENTITY_UNRESOLVED,
    BusinessFitSignal,
    CompletenessSignal,
    Deliverability,
    DeliverabilitySignal,
    DimensionScore,
    Eligibility,
    EligibilityInput,
    EligibilityOutcome,
    FreshnessSignal,
    IdentitySignal,
    QualityDimension,
    QualityEvidence,
    QualityLevel,
    QualitySummary,
    completeness_from_observations,
    freshness_from_observations,
    summarize_quality,
)

__all__ = [
    "DEFAULT_AGING_MAX_DAYS",
    "DEFAULT_COMPLETE_MIN_RATIO",
    "DEFAULT_FIT_HIGH_MIN",
    "DEFAULT_FIT_MEDIUM_MIN",
    "DEFAULT_FRESH_MAX_DAYS",
    "DEFAULT_PARTIAL_MIN_RATIO",
    "REASON_COMPLETENESS_COMPLETE",
    "REASON_COMPLETENESS_NO_EXPECTED_FIELDS",
    "REASON_COMPLETENESS_PARTIAL",
    "REASON_COMPLETENESS_SPARSE",
    "REASON_DELIVERABILITY_CATCH_ALL",
    "REASON_DELIVERABILITY_INVALID",
    "REASON_DELIVERABILITY_NO_CONTACT_POINT",
    "REASON_DELIVERABILITY_UNVERIFIED",
    "REASON_DELIVERABILITY_VERIFIED",
    "REASON_FIT_HIGH",
    "REASON_FIT_LOW",
    "REASON_FIT_MEDIUM",
    "REASON_FIT_NO_SCORE",
    "REASON_FRESHNESS_AGING",
    "REASON_FRESHNESS_FRESH",
    "REASON_FRESHNESS_NO_OBSERVATIONS",
    "REASON_FRESHNESS_STALE",
    "REASON_IDENTITY_AMBIGUOUS",
    "REASON_IDENTITY_CORROBORATED",
    "REASON_IDENTITY_STRONG",
    "REASON_IDENTITY_UNRESOLVED",
    "BusinessFitSignal",
    "CompletenessSignal",
    "Deliverability",
    "DeliverabilitySignal",
    "DimensionScore",
    "Eligibility",
    "EligibilityInput",
    "EligibilityOutcome",
    "FreshnessSignal",
    "IdentitySignal",
    "QualityDimension",
    "QualityEvidence",
    "QualityLevel",
    "QualitySummary",
    "completeness_from_observations",
    "freshness_from_observations",
    "summarize_quality",
]
