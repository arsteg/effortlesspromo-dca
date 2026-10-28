"""DCA-070 — overlay an authorized user correction on top of the DCA-069 canonical selection.

This is the thin, pure, network-free layer that sits ABOVE the DCA-069 provider-observation selection
(:mod:`app.canonical.field_selection`). For ONE subject + ONE ``field_key`` it takes the provider
:class:`~app.canonical.field_selection.CanonicalSelection` (what the provider evidence would make
canonical) and an optional authorized user :class:`UserCorrection`, and decides what the business
actually treats as canonical — giving a *protected* user correction precedence over any provider value.

The load-bearing guarantee (the DCA-070 acceptance criterion):

- **A refresh does not overwrite a protected correction.** When a protected user correction exists its
  value is canonical, REGARDLESS of how fresh/reliable the provider selection's winner is — so
  processing a newer provider observation (which changes the DCA-069 provider winner) does NOT change
  the field the business sees. The correction and the losing provider observations ALL remain in
  history; this layer, like the selector beneath it, records / mutates / deletes nothing. The reason
  codes make the outcome explainable, including :data:`REASON_REFRESH_DID_NOT_OVERWRITE` when a
  protected correction held its value over a DIFFERENT provider winner.

- **Business-private.** The overlay reads only the correction it is handed. A caller supplies a
  business's own active correction (from the tenant-scoped
  :class:`~app.services.field_correction.FieldCorrectionStore`); a different business has none, so the
  overlay returns that business's provider selection unchanged — the same subject/field can show the
  corrected value in one business and the provider value in another.

An UNprotected correction (the user explicitly opted out of refresh protection) does NOT override a
provider refresh: the provider selection wins and :data:`REASON_CORRECTION_NOT_PROTECTED` is recorded.
Acting on the canonical value — promoting it into any shared/host record — stays a disabled future
capability; this layer only computes and explains the choice at a given ``now``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.canonical.field_selection import CanonicalSelection

if TYPE_CHECKING:
    from app.db.models import FieldCorrection

# Where the canonical value came from after the overlay.
ORIGIN_USER_CORRECTION = "user_correction"
ORIGIN_PROVIDER_SELECTION = "provider_selection"
ORIGIN_NONE = "none"

# Stable reason codes explaining the overlay outcome (explainability, like the DCA-069 REASON_* codes).
REASON_USER_CORRECTION_PROTECTED = "user_correction_protected"
# The load-bearing acceptance reason: a protected correction held its value over a provider selection
# whose winner was a DIFFERENT value — i.e. a refresh did NOT overwrite the user's protected value.
REASON_REFRESH_DID_NOT_OVERWRITE = "refresh_did_not_overwrite"
REASON_CORRECTION_NOT_PROTECTED = "correction_not_protected"
REASON_NO_USER_CORRECTION = "no_user_correction"


@dataclass(frozen=True)
class UserCorrection:
    """One authorized user correction as the overlay sees it — a value object, never tenant state.

    Build one from a stored row with :func:`correction_from_row` (the caller reads it within its tenant
    scope, so the overlay never widens scope). ``protected`` is the default-True refresh protection.
    """

    correction_id: str
    field_key: str
    value: str
    protected: bool = True
    version: int = 0


@dataclass(frozen=True)
class CanonicalOutcome:
    """The explainable canonical value for ONE field_key AFTER applying the user-correction overlay.

    - ``value`` — the value the business treats as canonical, or ``None`` when neither a correction nor
      an eligible provider value exists.
    - ``origin`` — :data:`ORIGIN_USER_CORRECTION`, :data:`ORIGIN_PROVIDER_SELECTION` or
      :data:`ORIGIN_NONE`.
    - ``reasons`` — stable reason codes explaining the outcome.
    - ``correction`` — the user correction considered (or ``None``).
    - ``provider_selection`` — the underlying DCA-069 provider selection (carried for full
      explainability; its winner / verdicts / preserved losers are untouched).
    - ``mutated_state`` — always ``False``; the overlay is a pure conclusion.
    """

    field_key: str
    value: str | None
    origin: str
    reasons: tuple[str, ...]
    correction: UserCorrection | None
    provider_selection: CanonicalSelection
    mutated_state: bool = False

    @property
    def refresh_protected(self) -> bool:
        """True when a protected user correction is what made the canonical value business-private."""
        return self.origin == ORIGIN_USER_CORRECTION


def correction_from_row(correction: "FieldCorrection | None") -> UserCorrection | None:
    """Map a stored :class:`~app.db.models.FieldCorrection` row onto a read-only :class:`UserCorrection`
    (or ``None`` when there is no active correction). Builds a value object only — reads nothing else,
    mutates nothing."""
    if correction is None:
        return None
    return UserCorrection(
        correction_id=correction.id,
        field_key=correction.field_key,
        value=correction.corrected_value,
        protected=bool(correction.protected_from_refresh),
        version=correction.version,
    )


def apply_user_correction(
    provider_selection: CanonicalSelection,
    correction: UserCorrection | None = None,
) -> CanonicalOutcome:
    """Overlay an authorized user correction on a DCA-069 provider selection (pure, explainable).

    - A **protected** correction is canonical: its value wins over any provider value, so a newer
      provider observation cannot overwrite it (:data:`REASON_USER_CORRECTION_PROTECTED`, plus
      :data:`REASON_REFRESH_DID_NOT_OVERWRITE` when the provider winner was a different value).
    - An **unprotected** correction does not override a refresh: the provider selection wins
      (:data:`REASON_CORRECTION_NOT_PROTECTED`).
    - With **no** correction the provider selection is used
      (:data:`REASON_NO_USER_CORRECTION`); the value is ``None`` when the provider has no eligible
      winner either.

    Mutates nothing; the provider selection (its winner, verdicts and preserved losers) is carried
    through unchanged.
    """
    if correction is not None and correction.field_key and provider_selection.field_key:
        if correction.field_key != provider_selection.field_key:
            raise ValueError(
                "apply_user_correction expects the correction and provider selection to concern the "
                f"same field_key; got '{correction.field_key}' vs '{provider_selection.field_key}'"
            )

    field_key = provider_selection.field_key or (correction.field_key if correction else "")
    provider_value = (
        provider_selection.winner.value if provider_selection.winner is not None else None
    )

    if correction is not None and correction.protected:
        reasons = [REASON_USER_CORRECTION_PROTECTED]
        # A refresh "did not overwrite" when the provider had a winner and it is a DIFFERENT value than
        # the user's protected correction — the acceptance-defining case.
        if provider_value is not None and provider_value != correction.value:
            reasons.append(REASON_REFRESH_DID_NOT_OVERWRITE)
        return CanonicalOutcome(
            field_key=field_key,
            value=correction.value,
            origin=ORIGIN_USER_CORRECTION,
            reasons=tuple(reasons),
            correction=correction,
            provider_selection=provider_selection,
        )

    # No correction, or an unprotected one: the provider selection governs (refresh applies).
    reason = (
        REASON_CORRECTION_NOT_PROTECTED if correction is not None else REASON_NO_USER_CORRECTION
    )
    origin = ORIGIN_PROVIDER_SELECTION if provider_value is not None else ORIGIN_NONE
    return CanonicalOutcome(
        field_key=field_key,
        value=provider_value,
        origin=origin,
        reasons=(reason,),
        correction=correction,
        provider_selection=provider_selection,
    )
