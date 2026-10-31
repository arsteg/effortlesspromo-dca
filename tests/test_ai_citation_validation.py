"""DCA-133 — validate AI-extracted fact citations against the DCA-retrieved evidence.

Focused tests over the pure, deterministic citation validator: a grounded fact passes; a fact citing
evidence the DCA never retrieved (invented), a fact quoting text absent from the source (hallucinated),
a fact with no citation, a fact on a non-permitted field (unrelated), and a fact resting on expired
source rights are each REJECTED with a stable reason and change nothing; an unknown (null-value) fact
stays null and is dropped; an inferred value is accepted while labelled inferred; and the result is
deterministic. No AI, no network, no DB, no spend.
"""

from __future__ import annotations

from app.ai import (
    CitedFact,
    CitedSource,
    validate_citations,
    validate_fact_citation,
)
from app.ai.citation_validator import (
    LABEL_INFERRED,
    LABEL_OBSERVED,
    REASON_CITATION_NOT_IN_SOURCE,
    REASON_FIELD_NOT_PERMITTED,
    REASON_INVALID_INFERENCE_LABEL,
    REASON_MISSING_CITATION,
    REASON_SOURCE_RIGHTS_EXPIRED,
    REASON_UNKNOWN_EVIDENCE,
    REASON_UNKNOWN_FACT,
    REASON_VALUE_NOT_SUPPORTED,
)

# A synthetic retrieved page the DCA actually captured — the only evidence a fact may cite.
PAGE_CONTENT = (
    "About Acme Textiles Pvt Ltd.\n"
    "Legal name: Acme Textiles Private Limited.\n"
    "Industry: Textiles manufacturing.\n"
    "We operate three mills across Maharashtra."
)

SOURCE = CitedSource(
    ref="evidence-1",
    content=PAGE_CONTENT,
    permitted_fields=frozenset({"legal_name", "industry", "description"}),
    rights_valid=True,
)


def _one(fact: CitedFact, sources=(SOURCE,)):
    """Validate a single fact against the default source set and return its sole verdict."""
    return validate_citations([fact], list(sources)).verdicts[0]


# --------------------------------------------------------------------------------------------------
# A grounded fact passes
# --------------------------------------------------------------------------------------------------


def test_grounded_observed_fact_is_accepted():
    fact = CitedFact(
        field_key="legal_name",
        field_value="Acme Textiles Private Limited",
        citation_ref="evidence-1",
        cited_text="Legal name: Acme Textiles Private Limited.",
        inference_label=LABEL_OBSERVED,
    )
    verdict = _one(fact)
    assert verdict.accepted is True
    assert verdict.reason is None
    assert verdict.grounded_ref == "evidence-1"


def test_grounding_is_whitespace_and_case_insensitive():
    # The citation differs from the source only by case and spacing — it still grounds.
    fact = CitedFact(
        field_key="industry",
        field_value="textiles   MANUFACTURING",
        citation_ref="evidence-1",
        cited_text="industry:  textiles manufacturing",
        inference_label=LABEL_OBSERVED,
    )
    assert _one(fact).accepted is True


def test_inferred_value_accepted_while_labelled_inferred():
    # The value ("manufacturer") is DERIVED — it is not quoted verbatim — but its cited basis appears in
    # the source and the label is `inferred`, so it is accepted without being passed off as observed.
    fact = CitedFact(
        field_key="description",
        field_value="manufacturer",
        citation_ref="evidence-1",
        cited_text="Industry: Textiles manufacturing.",
        inference_label=LABEL_INFERRED,
    )
    verdict = _one(fact)
    assert verdict.accepted is True
    assert verdict.grounded_ref == "evidence-1"


# --------------------------------------------------------------------------------------------------
# Invented / unrelated / hallucinated citations are rejected
# --------------------------------------------------------------------------------------------------


def test_invented_evidence_ref_is_rejected():
    fact = CitedFact(
        field_key="legal_name",
        field_value="Acme Textiles Private Limited",
        citation_ref="evidence-does-not-exist",
        cited_text="Legal name: Acme Textiles Private Limited.",
    )
    verdict = _one(fact)
    assert verdict.accepted is False
    assert verdict.reason == REASON_UNKNOWN_EVIDENCE
    assert verdict.grounded_ref is None


def test_hallucinated_citation_text_absent_from_source_is_rejected():
    fact = CitedFact(
        field_key="legal_name",
        field_value="Globex Corporation",
        citation_ref="evidence-1",
        cited_text="Legal name: Globex Corporation.",  # never appears in PAGE_CONTENT
    )
    verdict = _one(fact)
    assert verdict.accepted is False
    assert verdict.reason == REASON_CITATION_NOT_IN_SOURCE


def test_observed_value_not_supported_by_its_cited_span_is_rejected():
    # The cited span really appears in the source, but it does not contain the asserted value.
    fact = CitedFact(
        field_key="industry",
        field_value="pharmaceuticals",
        citation_ref="evidence-1",
        cited_text="Industry: Textiles manufacturing.",
        inference_label=LABEL_OBSERVED,
    )
    verdict = _one(fact)
    assert verdict.accepted is False
    assert verdict.reason == REASON_VALUE_NOT_SUPPORTED


def test_unrelated_field_not_permitted_is_rejected():
    # `cin` is genuinely quoted in a span... but the source is not permitted to supply it.
    source = CitedSource(
        ref="evidence-1",
        content=PAGE_CONTENT + "\nCIN: U17110MH1990PTC055555",
        permitted_fields=frozenset({"legal_name", "industry"}),
        rights_valid=True,
    )
    fact = CitedFact(
        field_key="cin",
        field_value="U17110MH1990PTC055555",
        citation_ref="evidence-1",
        cited_text="CIN: U17110MH1990PTC055555",
    )
    verdict = _one(fact, sources=(source,))
    assert verdict.accepted is False
    assert verdict.reason == REASON_FIELD_NOT_PERMITTED


# --------------------------------------------------------------------------------------------------
# Missing citation, expired rights, unknown value, bad label
# --------------------------------------------------------------------------------------------------


def test_fact_with_no_citation_is_rejected():
    assert (
        _one(
            CitedFact(field_key="legal_name", field_value="Acme", citation_ref="", cited_text="")
        ).reason
        == REASON_MISSING_CITATION
    )
    # A ref with no supporting span is equally un-grounded.
    assert (
        _one(
            CitedFact(
                field_key="legal_name",
                field_value="Acme",
                citation_ref="evidence-1",
                cited_text="",
            )
        ).reason
        == REASON_MISSING_CITATION
    )


def test_expired_source_rights_cannot_ground_an_observation():
    expired = CitedSource(
        ref="evidence-1",
        content=PAGE_CONTENT,
        permitted_fields=frozenset({"legal_name"}),
        rights_valid=False,
    )
    fact = CitedFact(
        field_key="legal_name",
        field_value="Acme Textiles Private Limited",
        citation_ref="evidence-1",
        cited_text="Legal name: Acme Textiles Private Limited.",
    )
    verdict = _one(fact, sources=(expired,))
    assert verdict.accepted is False
    assert verdict.reason == REASON_SOURCE_RIGHTS_EXPIRED


def test_unknown_fact_with_null_value_stays_null_and_is_dropped():
    verdict = validate_fact_citation(
        CitedFact(field_key="gstin", field_value=None, citation_ref="evidence-1", cited_text="x"),
        {"evidence-1": SOURCE},
    )
    assert verdict.accepted is False
    assert verdict.reason == REASON_UNKNOWN_FACT
    # Blank (whitespace-only) is equally an unknown fact.
    blank = validate_fact_citation(
        CitedFact(field_key="gstin", field_value="   ", citation_ref="evidence-1", cited_text="x"),
        {"evidence-1": SOURCE},
    )
    assert blank.reason == REASON_UNKNOWN_FACT


def test_unrecognized_inference_label_is_rejected():
    fact = CitedFact(
        field_key="legal_name",
        field_value="Acme Textiles Private Limited",
        citation_ref="evidence-1",
        cited_text="Legal name: Acme Textiles Private Limited.",
        inference_label="fabricated",
    )
    assert _one(fact).reason == REASON_INVALID_INFERENCE_LABEL


# --------------------------------------------------------------------------------------------------
# Batch behaviour + determinism
# --------------------------------------------------------------------------------------------------


def test_batch_separates_grounded_from_ungrounded_without_affecting_each_other():
    grounded = CitedFact(
        field_key="legal_name",
        field_value="Acme Textiles Private Limited",
        citation_ref="evidence-1",
        cited_text="Legal name: Acme Textiles Private Limited.",
    )
    invented = CitedFact(
        field_key="legal_name",
        field_value="Globex",
        citation_ref="evidence-ghost",
        cited_text="Legal name: Globex.",
    )
    result = validate_citations([grounded, invented], [SOURCE])

    assert result.accepted == (grounded,)
    assert result.accepted_count == 1 and result.rejected_count == 1
    assert result.all_grounded is False
    # Verdicts are in input order; the rejection carries the invented fact + its reason.
    assert [v.accepted for v in result.verdicts] == [True, False]
    assert result.rejected[0].fact is invented
    assert result.rejected[0].reason == REASON_UNKNOWN_EVIDENCE


def test_validation_is_deterministic():
    facts = [
        CitedFact(
            field_key="legal_name",
            field_value="Acme Textiles Private Limited",
            citation_ref="evidence-1",
            cited_text="Legal name: Acme Textiles Private Limited.",
        ),
        CitedFact(
            field_key="industry",
            field_value="pharma",
            citation_ref="evidence-1",
            cited_text="Industry: Textiles manufacturing.",
        ),
    ]
    first = validate_citations(facts, [SOURCE])
    second = validate_citations(list(facts), [SOURCE])
    assert first == second


def test_empty_batch_is_trivially_all_grounded():
    result = validate_citations([], [SOURCE])
    assert result.verdicts == ()
    assert result.all_grounded is True
    assert result.accepted == ()
