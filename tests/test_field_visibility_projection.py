"""DCA-063 — filter fields by display entitlement: the pure projection logic.

Always-on unit tests (no database) pin the decision the route depends on:

- a **licensed shared fact** (a field with a ``source_key``) is visible ONLY when the display decider
  allows it — its value is carried; when display is NOT entitled the value is dropped (``value is
  None``) so the serializer omits it, and a stable reason is recorded;
- a **private overlay** (an inferred field with no ``source_key``) is always visible — the business's
  own data is never gated by a source-display entitlement;
- an **evidence object**'s raw payload is carried only when display is entitled AND the payload was
  retained; a ``reference_only`` object carries nothing even when entitled; the traceability metadata
  (digest, locator, retention mode) stays visible in every case.

These exercise :mod:`app.projection.field_visibility` with a trivial in-memory decider; the route-level
behaviour (raw JSON, tenant isolation, the real DCA-047 ``permits`` read) lives in
``tests/test_records_api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.models import EvidenceObject, EvidenceRetentionMode, FieldObservation
from app.projection.field_visibility import (
    REASON_DISPLAY_NOT_ENTITLED,
    display_decider_from_permits,
    is_private_overlay,
    project_evidence,
    project_field,
    project_fields,
)

OBSERVED_AT = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def _field(**over) -> FieldObservation:
    base = dict(
        id="f-1",
        business_id="A",
        subject_type="organization",
        subject_id="org-1",
        field_key="revenue_inr",
        field_value="120000000",
        inference_label="observed",
        source_key="mca_master_data",
        source_version=1,
        provenance_ref="synthetic://mca/record/42",
        observed_at=OBSERVED_AT,
        scope="nationwide",
        rights_basis="source_terms:display;purpose=prospecting",
    )
    base.update(over)
    return FieldObservation(**base)


def _evidence(**over) -> EvidenceObject:
    base = dict(
        id="e-1",
        business_id="A",
        source_key="mca_master_data",
        source_version=1,
        subject_type="organization",
        subject_id="org-1",
        content_kind="registry_record",
        content_ref="synthetic://mca/record/42",
        content_digest="d" * 64,
        content_media_type="application/json",
        raw_content='{"cin": "U17110KA1990PTC011089"}',
        retention_mode=EvidenceRetentionMode.RAW_RETAINED.value,
        retention_reason="",
    )
    base.update(over)
    return EvidenceObject(**base)


ALLOW = lambda sk, fk: (True, None)  # noqa: E731 — tiny test decider
DENY = lambda sk, fk: (False, "operation_not_entitled")  # noqa: E731


# --- licensed shared facts: the headline -----------------------------------------------------------


def test_licensed_fact_visible_when_display_entitled():
    projected = project_field(_field(), ALLOW)
    assert projected.visible is True
    assert projected.overlay is False
    assert projected.value == "120000000"
    assert projected.restricted_reason == ""


def test_licensed_fact_value_absent_when_display_not_entitled():
    projected = project_field(_field(), DENY)
    assert projected.visible is False
    assert projected.overlay is False
    # The restricted value is DROPPED — never carried — so the serializer omits it entirely.
    assert projected.value is None
    assert projected.restricted_reason == "operation_not_entitled"
    # Provenance stays so the denial is reviewable without leaking the value.
    assert projected.field_key == "revenue_inr"
    assert projected.source_key == "mca_master_data"


def test_decider_with_no_reason_falls_back_to_stable_code():
    projected = project_field(_field(), lambda sk, fk: (False, None))
    assert projected.visible is False
    assert projected.restricted_reason == REASON_DISPLAY_NOT_ENTITLED


# --- private overlays: the business's own data -----------------------------------------------------


def test_private_overlay_is_always_visible():
    # An inferred field with no originating source is the business's own overlay; never source-gated.
    overlay = _field(inference_label="inferred", source_key="", source_version=None)
    assert is_private_overlay(overlay) is True
    # Even with a DENY decider (which would refuse any licensed fact) the overlay is shown.
    projected = project_field(overlay, DENY)
    assert projected.overlay is True
    assert projected.visible is True
    assert projected.value == "120000000"
    assert projected.restricted_reason == ""


def test_inferred_field_from_a_named_source_is_still_a_licensed_fact():
    # An inference DERIVED from a named source is licensed, not a private overlay — it is still gated.
    derived = _field(inference_label="inferred", source_key="mca_master_data", source_version=1)
    assert is_private_overlay(derived) is False
    assert project_field(derived, DENY).visible is False


def test_project_fields_mixes_visible_overlays_and_restricted_facts():
    observations = [
        _field(field_key="revenue_inr"),  # licensed, DENY -> restricted
        _field(field_key="segment", inference_label="inferred", source_key="", source_version=None),
    ]
    projected = project_fields(observations, DENY)
    assert [p.visible for p in projected] == [False, True]
    assert projected[0].value is None
    assert projected[1].value == "120000000"


# --- evidence objects ------------------------------------------------------------------------------


def test_evidence_raw_payload_visible_when_display_entitled():
    projected = project_evidence(_evidence(), ALLOW)
    assert projected.raw_visible is True
    assert projected.raw_content == '{"cin": "U17110KA1990PTC011089"}'
    assert projected.restricted_reason == ""
    # Traceability metadata is always present.
    assert projected.content_digest == "d" * 64
    assert projected.content_ref == "synthetic://mca/record/42"


def test_evidence_raw_payload_absent_when_display_not_entitled():
    projected = project_evidence(_evidence(), DENY)
    assert projected.raw_visible is False
    assert projected.raw_content is None  # dropped — serializer omits it
    assert projected.restricted_reason == "operation_not_entitled"
    # Metadata still flows so the evidence stays traceable/reviewable.
    assert projected.content_digest == "d" * 64
    assert projected.retention_mode == EvidenceRetentionMode.RAW_RETAINED.value


def test_reference_only_evidence_carries_no_raw_even_when_entitled():
    ref_only = _evidence(
        raw_content=None,
        retention_mode=EvidenceRetentionMode.REFERENCE_ONLY.value,
        retention_reason="no_retention_policy",
    )
    projected = project_evidence(ref_only, ALLOW)
    assert projected.raw_visible is False
    assert projected.raw_content is None
    # Display WAS entitled; the absent payload is a DCA-062 retention outcome, not a display refusal.
    assert projected.restricted_reason == ""
    assert projected.content_digest == "d" * 64


# --- the permits adapter ---------------------------------------------------------------------------


def test_display_decider_binds_the_display_operation():
    seen: list[tuple] = []

    def permits(source_key, field_key, operation, *, now=None):
        seen.append((source_key, field_key, operation, now))
        return True, None

    decide = display_decider_from_permits(permits)
    assert decide("mca_master_data", "revenue_inr") == (True, None)
    assert seen == [("mca_master_data", "revenue_inr", "display", None)]
