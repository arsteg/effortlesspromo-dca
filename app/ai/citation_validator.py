"""DCA-133 — validate AI-extracted fact CITATIONS against the DCA-retrieved evidence (anti-hallucination).

Key: ``ai-grounding`` · source sections 4, 8, 9. Stage: existing AI-service integration.

**Load-bearing guarantee:** before any AI-extracted fact can become a :class:`~app.db.models.FieldObservation`,
every fact must CITE supporting material the DCA itself retrieved — and that citation is checked here,
independently, against the actual evidence content and the source's permitted fields. A fact whose
citation is invented (names evidence the DCA never retrieved), hallucinated (quotes text that does not
appear in the cited source), unrelated (asserts a field the source is not permitted to supply), or
rests on expired source rights is REJECTED: the fact is dropped, a stable reason is recorded, and it
changes NOTHING downstream (no observation, no evidence, no promotion). Only a fact whose citation is
genuinely supported passes. An AI-extracted value the model could not find stays NULL and is dropped
(never guessed), and an INFERRED value is accepted only while it stays LABELLED inferred (its cited
basis must still appear in the source) — an inference is never passed off as a directly observed fact.

This is the DCA-side counterpart of the effortlesspromo-ai gateway's ``claim_grounding`` screen
(``gateway/validation.py``), mirroring its intent — "a claim is grounded only if its text is covered by
the approved fact set; there is no override path, a failure is a hard block" — but applied the other way
round: the gateway screens generated COPY against approved facts, while this screens extracted FACTS
against the DCA-retrieved SOURCE. The two are deliberately separate gates: **the gateway's own
validation verdict cannot authorize an observation write.** Even an envelope the gateway marked
``valid`` must pass this grounding gate before a fact may be written — a gateway "valid" says the output
was well-formed and safe, NOT that each extracted fact is actually supported by evidence this tenant was
entitled to collect. The DCA owns that second decision because only the DCA holds the retrieved evidence
and the source-rights state.

This module is a **pure, deterministic** screen — no I/O, no model/paid work, no DB access, no mutation
of its input. It re-implements none of the gateway defenses and routes nothing to a model; it simply
confirms, by a case-insensitive / whitespace-normalised substring check (the same crude, deterministic
entailment proxy the gateway's grounding screen uses), that each citation is real:

1. **A citation is mandatory for an observed fact (fail-closed).** A fact with no cited evidence ref or
   no cited span is REJECTED (:data:`REASON_MISSING_CITATION`) — an unprovenanced value is never
   promoted, exactly as the DCA-061 field-observation store refuses a bare value with no provenance.
2. **The cited evidence must be one the DCA actually retrieved.** A fact citing an evidence ref not
   present in the supplied retrieved-evidence set is an INVENTED citation and is REJECTED
   (:data:`REASON_UNKNOWN_EVIDENCE`).
3. **Expired source rights cannot ground an accepted observation.** If the cited evidence's source
   rights are no longer in force (``rights_valid=False``, derived by the caller from the DCA-044..048
   source-rights chain), the fact is REJECTED (:data:`REASON_SOURCE_RIGHTS_EXPIRED`) — a fact can never
   be accepted on evidence the tenant is no longer entitled to use.
4. **Only a permitted field may be collected from a source.** A fact whose field is not on the cited
   source's permitted-field allowlist is an UNRELATED citation and is REJECTED
   (:data:`REASON_FIELD_NOT_PERMITTED`).
5. **The cited span must actually appear in the cited source content.** A quote not found in the
   retrieved content is a HALLUCINATED citation and is REJECTED (:data:`REASON_CITATION_NOT_IN_SOURCE`).
6. **An observed value must be supported by its own cited span.** For an ``observed`` fact the asserted
   value must itself appear within the cited span; a span that does not contain the value does not
   support it (:data:`REASON_VALUE_NOT_SUPPORTED`). An ``inferred`` fact skips this check — its value is
   derived, not quoted — but it must still be LABELLED inferred and its cited basis must appear in the
   source (checks 1–5 still apply), so an inference is never dressed up as an observed fact.

A fact whose value is NULL/blank is DROPPED as an unknown fact (:data:`REASON_UNKNOWN_FACT`), not an
error — "unknown facts stay null" — and an unrecognised inference label is REJECTED
(:data:`REASON_INVALID_INFERENCE_LABEL`). Every fact, accepted or dropped, gets a
:class:`FactVerdict` recording the decision and (on rejection) the stable reason, so a caller / audit
line sees exactly why each extracted fact was or was not allowed to become an observation. The caller
owns the write: this validator only decides WHICH facts are grounded; it performs no observation write
itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# The inference-label vocabulary, mirroring :class:`~app.db.models.FieldInferenceLabel` (kept as local
# constants so this pure AI-seam screen does not import the DB models). An observed value was retrieved
# directly from the source and must be quoted by its citation; an inferred value was derived and is only
# ever accepted while LABELLED inferred.
LABEL_OBSERVED = "observed"
LABEL_INFERRED = "inferred"
_KNOWN_LABELS: frozenset[str] = frozenset({LABEL_OBSERVED, LABEL_INFERRED})

# Stable rejection/drop reason codes (fail-closed, test-asserted — change deliberately).
REASON_UNKNOWN_FACT = "unknown_fact_null"  # value is null/blank: stays null, dropped (not an error)
REASON_INVALID_INFERENCE_LABEL = "invalid_inference_label"
REASON_MISSING_CITATION = "citation_missing"  # no cited evidence ref or no cited span
REASON_UNKNOWN_EVIDENCE = (
    "citation_evidence_unknown"  # cites evidence the DCA never retrieved (invented)
)
REASON_SOURCE_RIGHTS_EXPIRED = (
    "source_rights_expired"  # cited source's rights are no longer in force
)
REASON_FIELD_NOT_PERMITTED = "field_not_permitted"  # field not on the source's permitted allowlist
REASON_CITATION_NOT_IN_SOURCE = (
    "citation_not_in_source"  # cited span absent from the source (hallucinated)
)
REASON_VALUE_NOT_SUPPORTED = (
    "value_not_supported"  # observed value not contained in its own cited span
)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lower-case and collapse all whitespace runs to single spaces — the deterministic match basis.

    Nothing in ``text`` is parsed-as-code, decoded or evaluated; this only folds case and whitespace so
    a citation that differs from the source only by spacing/newlines/case still matches (the same crude
    proxy the gateway's grounding screen uses). Pure and total.
    """
    return _WHITESPACE_RE.sub(" ", (text or "")).strip().lower()


def _contains(haystack: str, needle: str) -> bool:
    """True when ``needle`` appears in ``haystack`` under :func:`_normalize`. A blank needle is never a match."""
    normalized_needle = _normalize(needle)
    if not normalized_needle:
        return False
    return normalized_needle in _normalize(haystack)


@dataclass(frozen=True)
class CitedSource:
    """One piece of evidence the DCA ACTUALLY retrieved, against which a fact's citation is checked.

    ``ref`` is the stable reference a fact cites (e.g. an :class:`~app.db.models.EvidenceObject` id or
    its ``content_ref`` locator). ``content`` is the retrieved source text the cited span must appear in
    — exactly what the DCA captured, treated as inert data (scanned only, never executed). ``permitted_fields``
    is the allowlist of field keys this source may supply (e.g. the DCA-124 organization-field allowlist);
    a field outside it is an unrelated citation. ``rights_valid`` is whether the source's rights are still
    in force (the caller derives it from the DCA-044..048 source-rights chain); expired rights can ground
    nothing. This validator reads the source; it never fetches it.
    """

    ref: str
    content: str
    permitted_fields: frozenset[str] = frozenset()
    rights_valid: bool = True


@dataclass(frozen=True)
class CitedFact:
    """One AI-extracted fact plus the citation the model offered for it.

    ``field_key`` / ``field_value`` are the extracted field and its value; ``field_value`` is ``None``
    (or blank) for an UNKNOWN fact the model could not find — it stays null and is dropped, never
    guessed. ``citation_ref`` names the :class:`CitedSource` the fact is grounded in, and ``cited_text``
    is the exact span the model claims supports the value (for an inferred fact, the basis it was derived
    from). ``inference_label`` is ``observed`` | ``inferred`` — an observed value must be quoted by its
    citation, an inferred value is derived but must stay labelled inferred.
    """

    field_key: str
    field_value: str | None
    citation_ref: str = ""
    cited_text: str = ""
    inference_label: str = LABEL_OBSERVED


@dataclass(frozen=True)
class FactVerdict:
    """The deterministic per-fact decision: whether the fact's citation is supported, and why not.

    ``accepted`` is True only for a genuinely grounded fact (one that may become an observation).
    ``reason`` is ``None`` when accepted and otherwise one of the stable ``REASON_*`` codes.
    ``grounded_ref`` is the cited evidence ref that grounds an accepted fact (``None`` on rejection or
    on an unknown-null drop). The original ``fact`` is preserved so a caller / audit line retains the
    full picture without re-deriving it.
    """

    fact: CitedFact
    accepted: bool
    reason: str | None = None
    grounded_ref: str | None = None

    @property
    def dropped(self) -> bool:
        """True for any fact that will NOT become an observation (rejected or unknown-null)."""
        return not self.accepted


@dataclass(frozen=True)
class CitationValidationResult:
    """The outcome of validating a batch of extracted facts' citations — verdicts in input order.

    ``verdicts`` is one :class:`FactVerdict` per input fact, in order. :attr:`accepted` are the facts
    whose citations are supported (the only facts a caller may promote to observations); :attr:`rejected`
    are the verdicts for every dropped fact (with its reason). A rejection changes nothing downstream.
    """

    verdicts: tuple[FactVerdict, ...]

    @property
    def accepted(self) -> tuple[CitedFact, ...]:
        """The grounded facts — the only ones that may become observations."""
        return tuple(v.fact for v in self.verdicts if v.accepted)

    @property
    def rejected(self) -> tuple[FactVerdict, ...]:
        """The verdicts for every dropped fact (rejected citation or unknown-null), with reasons."""
        return tuple(v for v in self.verdicts if not v.accepted)

    @property
    def accepted_count(self) -> int:
        return sum(1 for v in self.verdicts if v.accepted)

    @property
    def rejected_count(self) -> int:
        return sum(1 for v in self.verdicts if not v.accepted)

    @property
    def all_grounded(self) -> bool:
        """True when every input fact was accepted (none dropped) — an empty batch is trivially True."""
        return all(v.accepted for v in self.verdicts)


def _index_sources(
    sources: Sequence[CitedSource] | Mapping[str, CitedSource],
) -> dict[str, CitedSource]:
    """Index the retrieved evidence by ``ref`` for a deterministic lookup (later refs never override)."""
    if isinstance(sources, Mapping):
        return {str(ref): src for ref, src in sources.items()}
    index: dict[str, CitedSource] = {}
    for src in sources:
        ref = (src.ref or "").strip()
        if ref and ref not in index:
            index[ref] = src
    return index


def validate_fact_citation(
    fact: CitedFact,
    sources: Mapping[str, CitedSource],
) -> FactVerdict:
    """Decide whether ONE extracted fact's citation is supported by the DCA-retrieved evidence.

    Pure and deterministic; checks run in a fixed order and stop at the first failure, which names the
    stable reason. See the module docstring for the full guarantee. Never mutates its inputs, performs no
    I/O, and writes no observation — it only returns the verdict.
    """
    # 0) An unknown fact (no value) stays NULL and is dropped — never guessed. Not an error.
    value = fact.field_value
    if value is None or not str(value).strip():
        return FactVerdict(fact=fact, accepted=False, reason=REASON_UNKNOWN_FACT)

    # 1) The inference label must be part of the controlled vocabulary (observed | inferred).
    label = (fact.inference_label or "").strip().lower()
    if label not in _KNOWN_LABELS:
        return FactVerdict(fact=fact, accepted=False, reason=REASON_INVALID_INFERENCE_LABEL)

    # 2) A citation is mandatory: both an evidence ref AND a cited span must be present.
    citation_ref = (fact.citation_ref or "").strip()
    cited_text = (fact.cited_text or "").strip()
    if not citation_ref or not cited_text:
        return FactVerdict(fact=fact, accepted=False, reason=REASON_MISSING_CITATION)

    # 3) The cited evidence must be one the DCA actually retrieved (else the citation is invented).
    source = sources.get(citation_ref)
    if source is None:
        return FactVerdict(fact=fact, accepted=False, reason=REASON_UNKNOWN_EVIDENCE)

    # 4) Expired source rights can ground nothing.
    if not source.rights_valid:
        return FactVerdict(fact=fact, accepted=False, reason=REASON_SOURCE_RIGHTS_EXPIRED)

    # 5) The field must be one the source is permitted to supply (else the citation is unrelated).
    if fact.field_key not in source.permitted_fields:
        return FactVerdict(fact=fact, accepted=False, reason=REASON_FIELD_NOT_PERMITTED)

    # 6) The cited span must actually appear in the retrieved source content (else it is hallucinated).
    if not _contains(source.content, cited_text):
        return FactVerdict(fact=fact, accepted=False, reason=REASON_CITATION_NOT_IN_SOURCE)

    # 7) For an OBSERVED fact, the asserted value must be supported by its own cited span. An INFERRED
    #    fact's value is derived (not quoted), so it is not required to appear verbatim — but its cited
    #    basis has already been proven to appear in the source above, and it stays labelled inferred.
    if label == LABEL_OBSERVED and not _contains(cited_text, str(value)):
        return FactVerdict(fact=fact, accepted=False, reason=REASON_VALUE_NOT_SUPPORTED)

    return FactVerdict(fact=fact, accepted=True, reason=None, grounded_ref=citation_ref)


def validate_citations(
    facts: Sequence[CitedFact],
    sources: Sequence[CitedSource] | Mapping[str, CitedSource],
) -> CitationValidationResult:
    """Validate every extracted fact's citation against the DCA-retrieved evidence — pure, deterministic.

    Returns a :class:`CitationValidationResult` with one :class:`FactVerdict` per input fact, in order.
    Only :attr:`CitationValidationResult.accepted` facts are grounded and may be promoted to
    observations by the caller; a rejected/dropped fact changes nothing. This is an INDEPENDENT gate from
    the gateway's own output validation — a gateway-"valid" envelope does not authorize a write; a fact
    becomes eligible only once its citation is confirmed supported here.
    """
    index = _index_sources(sources)
    verdicts = tuple(validate_fact_citation(fact, index) for fact in facts)
    return CitationValidationResult(verdicts=verdicts)


__all__ = [
    "LABEL_OBSERVED",
    "LABEL_INFERRED",
    "REASON_UNKNOWN_FACT",
    "REASON_INVALID_INFERENCE_LABEL",
    "REASON_MISSING_CITATION",
    "REASON_UNKNOWN_EVIDENCE",
    "REASON_SOURCE_RIGHTS_EXPIRED",
    "REASON_FIELD_NOT_PERMITTED",
    "REASON_CITATION_NOT_IN_SOURCE",
    "REASON_VALUE_NOT_SUPPORTED",
    "CitedSource",
    "CitedFact",
    "FactVerdict",
    "CitationValidationResult",
    "validate_fact_citation",
    "validate_citations",
]
