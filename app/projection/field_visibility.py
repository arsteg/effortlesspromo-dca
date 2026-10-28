"""DCA-063 — filter fields by display entitlement (rights-aware projection).

This is the single, deterministic *projection* that decides what a business is permitted to SEE when
a record or an evidence object is served back to it. It is applied at the API boundary so a restricted
value is **absent from the payload**, never merely hidden by a consumer: the serializer is handed a
:class:`ProjectedField` / :class:`ProjectedEvidence` that only carries the value when display is
entitled, so a client that ignores the ``restricted`` flag still receives no restricted value.

The load-bearing guarantee (the DCA-063 acceptance criterion):

- **A licensed shared fact is shown only when the business holds a DISPLAY entitlement for it.** A
  field observed from a catalogued :class:`~app.db.models.Source` (a *licensed shared fact*) carries
  the source licence; its value may be displayed back to the business only when that business's
  in-force DCA-047 entitlement grants the ``display`` operation for that ``(source, field)`` —
  deny-by-default (no entitlement, a disabled entitlement, or a withheld/un-approved right all deny).
  When display is NOT entitled the value is dropped (``value`` stays ``None`` and is never serialized)
  while the field's provenance — key, source, label, scope — stays visible so the denial is itself
  reviewable and reproducible.

- **A private overlay is the business's own data and is always displayed to it.** A field the business
  itself derived with no originating source (``inference_label == inferred`` and no ``source_key``) is
  not licensed from any source, so no source-display entitlement gates it — it is this tenant's own
  overlay on the shared facts and is always visible to the owning business.

The decision is delegated to a :data:`DisplayDecider` — ``(source_key, field_key) -> (allowed,
reason)`` — so this module stays a pure, side-effect-free projection with no database or tenancy
knowledge of its own. The route wires the DCA-047
:meth:`~app.services.source_entitlement.BusinessSourceEntitlementStore.permits` read (with the
``display`` operation) as the decider; a test can wire a trivial one. Nothing here mutates state, so a
result and its denial are fully reproducible with no side effects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from app.db.models import EvidenceObject, FieldObservation, SourceOperation

# A display-entitlement decision for one licensed (source, field): ``(allowed, reason)``. ``reason`` is
# a stable code naming WHY display was refused (``None`` when allowed). Deny-by-default is the decider's
# responsibility — this module treats any ``allowed is False`` as "do not serialize the value".
DisplayDecider = Callable[[str, str], tuple[bool, str | None]]

# The single operation this projection screens on — the DCA-045 ``display`` right.
DISPLAY_OPERATION = SourceOperation.DISPLAY.value

# The stable reason recorded on a restricted licensed fact when the decider gives no code of its own.
REASON_DISPLAY_NOT_ENTITLED = "display_not_entitled"


@dataclass(frozen=True)
class ProjectedField:
    """One field observation projected through the display-entitlement screen.

    ``value`` is populated ONLY when the field is visible — a restricted licensed fact leaves it
    ``None`` so the serializer omits the value entirely (the value is absent from the payload, not
    nulled-then-hidden). ``overlay`` marks a private overlay (the business's own, un-sourced,
    always-visible data). ``restricted_reason`` is the stable code naming why a non-visible licensed
    fact was withheld (``""`` for a visible field). The provenance fields stay populated in BOTH cases
    so a denial is reviewable without leaking the value.
    """

    field_key: str
    inference_label: str
    source_key: str
    source_version: int | None
    scope: str
    observed_at: datetime | None
    overlay: bool
    visible: bool
    value: str | None
    restricted_reason: str


@dataclass(frozen=True)
class ProjectedEvidence:
    """One evidence object projected through the display-entitlement screen.

    The traceability metadata (locator, digest, kind, retention mode) stays visible in both cases — it
    is provenance, not the licensed content. ``raw_content`` is populated ONLY when the business is
    display-entitled for ``(source_key, content_kind)`` AND the payload was actually retained (a
    DCA-062 ``reference_only`` object never held one). Otherwise ``raw_content`` stays ``None`` and
    ``restricted_reason`` names why it is withheld.
    """

    evidence_id: str
    source_key: str
    source_version: int
    subject_type: str
    subject_id: str
    content_kind: str
    content_ref: str
    content_digest: str
    content_media_type: str
    retention_mode: str
    retention_reason: str
    raw_visible: bool
    raw_content: str | None
    restricted_reason: str


def is_private_overlay(observation: FieldObservation) -> bool:
    """True if ``observation`` is a private overlay — the business's own data, not a licensed fact.

    A private overlay is an INFERRED field with no originating ``source_key``: the business derived it
    itself, so it is not licensed from any source and no source-display entitlement gates it. A field
    that names a source (an observed fact, or an inference derived from a named source) is a licensed
    shared fact and is screened.
    """
    return not (observation.source_key or "").strip()


def project_field(observation: FieldObservation, decide: DisplayDecider) -> ProjectedField:
    """Project one field observation: carry the value only when display is entitled.

    A private overlay (:func:`is_private_overlay`) is always visible — it is the business's own data.
    A licensed shared fact is visible only when ``decide(source_key, field_key)`` allows the
    ``display`` operation; otherwise the value is dropped (never serialized) and the refusal reason is
    recorded. Pure and side-effect-free.
    """
    source_key = (observation.source_key or "").strip()

    def _build(*, overlay: bool, visible: bool, value: str | None, reason: str) -> ProjectedField:
        return ProjectedField(
            field_key=observation.field_key,
            inference_label=observation.inference_label,
            source_key=source_key,
            source_version=observation.source_version,
            scope=observation.scope,
            observed_at=observation.observed_at,
            overlay=overlay,
            visible=visible,
            value=value,
            restricted_reason=reason,
        )

    if is_private_overlay(observation):
        # The business's own un-sourced data — always visible to its owner.
        return _build(overlay=True, visible=True, value=observation.field_value, reason="")

    allowed, reason = decide(source_key, observation.field_key)
    if allowed:
        return _build(overlay=False, visible=True, value=observation.field_value, reason="")
    # Display not entitled: the value is DROPPED (never carried) so it is absent from the payload.
    return _build(
        overlay=False,
        visible=False,
        value=None,
        reason=reason or REASON_DISPLAY_NOT_ENTITLED,
    )


def project_fields(
    observations: Iterable[FieldObservation], decide: DisplayDecider
) -> list[ProjectedField]:
    """Project a record's field observations through the display-entitlement screen, in order."""
    return [project_field(obs, decide) for obs in observations]


def project_evidence(evidence: EvidenceObject, decide: DisplayDecider) -> ProjectedEvidence:
    """Project one evidence object: carry ``raw_content`` only when display is entitled AND retained.

    The raw captured payload is a licensed shared fact, so it is served back only when the business is
    display-entitled for ``(source_key, content_kind)`` — otherwise it is dropped (never serialized).
    A DCA-062 ``reference_only`` object never held a raw payload, so there is nothing to carry even if
    display were entitled. The locator / digest / retention metadata stays visible as traceability.
    Pure and side-effect-free.
    """
    allowed, reason = decide(evidence.source_key, evidence.content_kind)
    has_raw = evidence.raw_content is not None
    raw_visible = bool(allowed and has_raw)
    if not allowed:
        restricted_reason = reason or REASON_DISPLAY_NOT_ENTITLED
    else:
        # Display is entitled; if there is simply no retained payload that is a DCA-062 outcome, not a
        # display refusal — record no restriction reason.
        restricted_reason = ""
    return ProjectedEvidence(
        evidence_id=evidence.id,
        source_key=evidence.source_key,
        source_version=evidence.source_version,
        subject_type=evidence.subject_type,
        subject_id=evidence.subject_id,
        content_kind=evidence.content_kind,
        content_ref=evidence.content_ref,
        content_digest=evidence.content_digest,
        content_media_type=evidence.content_media_type,
        retention_mode=evidence.retention_mode,
        retention_reason=evidence.retention_reason,
        raw_visible=raw_visible,
        raw_content=evidence.raw_content if raw_visible else None,
        restricted_reason=restricted_reason,
    )


def display_decider_from_permits(
    permits: Callable[..., tuple[bool, str | None]],
    *,
    now: datetime | None = None,
) -> DisplayDecider:
    """Adapt a DCA-047 ``permits(source_key, field, operation)`` read into a :data:`DisplayDecider`.

    Binds the ``display`` operation (and an optional evaluation ``now``) so the projection asks exactly
    "may this business DISPLAY this field of this source?" — the intersection of the platform ceiling
    and the business's own narrowing, deny-by-default.
    """

    def decide(source_key: str, field_key: str) -> tuple[bool, str | None]:
        return permits(source_key, field_key, DISPLAY_OPERATION, now=now)

    return decide
