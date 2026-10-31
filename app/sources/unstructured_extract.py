"""DCA-136 — extract permitted UNSTRUCTURED observations (the AI counterpart of DCA-124).

The single job of this story: turn ALREADY-FETCHED, APPROVED + entitled source CONTENT into permitted
ORGANIZATION :class:`~app.db.models.FieldObservation` rows, through the accepted DCA AI chokepoint, with
every extracted observation grounded in a citation the DCA itself re-validates before it is stored. It
is the AI counterpart of the deterministic DCA-124 website extractor
(:mod:`app.sources.website_extract`): same result (permitted company fields → provenanced observations
citing a captured :class:`~app.db.models.EvidenceObject`), but the fields are proposed by the model from
unstructured content instead of read off a fixed regex allowlist. It composes the already-accepted AI
seams end to end — it builds no second pipeline and performs no provider/model/paid work of its own:

- **Rights FIRST (DCA-044..048).** The fields the extraction may collect are resolved ONLY through the
  sanctioned :class:`~app.services.source_preflight.SourcePreflight` gate (``COLLECT`` on the source for
  the purpose). A source the business is not approved+entitled for yields NO permitted field, so the
  extraction refuses and NEVER calls the model — an unentitled source extracts nothing and spends
  nothing.
- **DCA-132 untrusted framing.** The subject kind + the permitted-field allowlist + the content ref go
  to the TRUSTED context; the content BODY goes ONLY to the untrusted channel
  (:func:`~app.ai.untrusted_framer.build_untrusted_request`), so injected content instructions arrive at
  the model as data, never as instructions.
- **DCA-128 facade.** The request runs through the ONE facade
  (:class:`~app.ai.contract.AiCapabilityFacade`) for the ``unstructured_observation_extraction``
  capability — the dev/test fixture transport today (no network, no spend), the live gateway only when
  configured. DCA-131 minimises the trusted context before egress.
- **DCA-132 boundary.** The gateway's returned validation / taint / injection / human-review signals are
  honoured (:func:`~app.ai.untrusted_framer.evaluate_untrusted_result`): a REJECTED output stores
  nothing.
- **DCA-133 independent citation grounding.** Each model-proposed observation is re-validated against the
  content the DCA actually retrieved and the source's permitted-field allowlist
  (:func:`~app.ai.citation_validator.validate_citations`). The gateway's own "valid" verdict does NOT
  authorize a write — only an observation whose citation is confirmed grounded AND whose field is
  permitted is kept. An invented / hallucinated / unrelated / non-permitted (incl. personal/contact) /
  expired-rights / unknown observation is DROPPED with a stable reason and changes nothing.
- **DCA-061 / DCA-062 writers.** The content is captured as a license-aware
  :class:`~app.db.models.EvidenceObject` (:class:`~app.services.evidence_store.EvidenceObjectStore`) and
  each grounded observation becomes a provenanced :class:`~app.db.models.FieldObservation`
  (:class:`~app.services.field_observation.FieldObservationStore`) that CITES that evidence — the two
  sanctioned writers, never bypassed. Nothing is auto-applied beyond being an observation (there is no
  canonical/confirmed write here).

Organization-only: the subject is fixed to an organization observation and the permitted fields are the
source's organization-field allowlist, so there is no path to a contact/personal field — a model
observation on one is dropped as not-permitted (DCA-133) and, at the gateway, fails the capability's own
``rule_check``.

The extraction is idempotent: a ``(source, subject, content)`` already extracted is a deterministic
no-op (the existing provenance row is found and skipped — no second model call, no duplicate
observations). The extractor stages into the caller's session and never commits — the caller owns the
unit of work, so the evidence, the field observations, their atomic audit and the extraction-provenance
row all commit together.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiContractViolation,
    CitedFact,
    CitedSource,
    ExternalContent,
    FactVerdict,
    UntrustedOutcome,
    build_untrusted_request,
    dca_job_binding,
    evaluate_untrusted_result,
    validate_citations,
)
from app.audit.sink import AuditActor
from app.db.models import (
    FieldInferenceLabel,
    FieldSubjectType,
    SourceOperation,
    UnstructuredObservationExtraction,
)
from app.services.evidence_store import EvidenceInput, EvidenceObjectStore
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import OrgObservationStore
from app.services.source_catalog import SourceCatalogStore
from app.services.source_preflight import PreflightRequest, SourcePreflight

# The capability key this story integrates (must be in the DCA-128 allowlist + the gateway registry).
CAPABILITY = "unstructured_observation_extraction"

# The source RIGHT (DCA-045 per-field operation) an extraction depends on, and the contract version the
# provenance rows are written under (expand→migrate→contract).
EXTRACT_OPERATION = SourceOperation.COLLECT.value
EXTRACT_SCHEMA_VERSION = 1

# The default content-kind recorded for the captured evidence object (DCA-062).
DEFAULT_CONTENT_KIND = "unstructured_content"

# Stable refusal codes — each returned BEFORE any observation/evidence is staged, so a denial changes
# nothing (and an unentitled source / disabled capability never even calls the model).
REASON_CAPABILITY_DISABLED = "unstructured_extract_capability_disabled"
REASON_OUTPUT_REJECTED = "unstructured_extract_output_rejected"
REASON_SOURCE_NOT_ENTITLED = "unstructured_extract_source_not_entitled"

# Request-shape / schema refusals — each raised BEFORE the session is mutated, so a refusal writes nothing.
REASON_MISSING_SOURCE_KEY = "request_missing_source_key"
REASON_MISSING_SOURCE_VERSION = "request_missing_source_version"
REASON_MISSING_PURPOSE = "request_missing_purpose"
REASON_MISSING_INVOCATION_KEY = "request_missing_invocation_key"
REASON_MISSING_CONTENT_REF = "request_missing_content_ref"
REASON_EMPTY_CONTENT = "content_body_empty"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"


class UnstructuredExtractError(Exception):
    """A request/schema problem refused the extraction with a stable code. Raised before the session is
    mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FetchedContent:
    """One piece of already-fetched / synthetic source content the adapter extracts from.

    ``content_ref`` is the stable locator the model cites and a later refresh re-locates by (also the
    idempotency handle). ``body`` is the UNTRUSTED content the model reads (framed untrusted, scanned for
    grounding, never executed). ``content_media_type`` is the declared type (advisory) and ``provenance``
    is the untrusted label carried on the untrusted channel (defaults to ``content_ref``). The adapter
    resolves / fetches nothing — the body stands in for what a (still-disabled) fetch would return.
    """

    content_ref: str
    body: str
    content_media_type: str = ""
    content_kind: str = DEFAULT_CONTENT_KIND
    provenance: str = ""

    def label(self) -> str:
        return (self.provenance or "").strip() or (self.content_ref or "").strip()


@dataclass(frozen=True)
class UnstructuredExtractRequest:
    """One would-be AI extraction for an organization observation the business already holds.

    ``subject_observation_id`` is the :class:`~app.db.models.OrgObservation` (in THIS business) the
    observations are about. ``source_key`` / ``source_version`` name the approved+entitled catalog source
    and its terms version the content is read under; ``purpose`` the purpose the extraction serves (rights
    are purpose-bound). ``invocation_key`` is the stable DCA-owned id the facade binding + the gateway
    idempotency key are derived from (so a retry replays, never double-spends). ``scope`` is the
    India-only collection scope recorded on each observation. ``rights_basis`` is the recorded rights
    basis (derived from the purpose when left blank).
    """

    subject_observation_id: str
    source_key: str
    source_version: int
    purpose: str
    invocation_key: str
    scope: str = "nationwide"
    rights_basis: str = ""


@dataclass(frozen=True)
class ExtractedObservation:
    """One grounded permitted organization observation the extraction kept: a field key + its value."""

    field_key: str
    field_value: str
    inference_label: str = FieldInferenceLabel.OBSERVED.value


@dataclass
class ExtractResult:
    """The inert, inspectable outcome of one AI extraction run (nothing fetched; inert on a denial).

    On a refusal ``refused_reason`` names the stable code and everything else is empty (nothing was
    written). On an idempotent replay ``skipped`` is True and ``evidence_id`` points at the existing
    evidence. On a fresh success ``recorded`` are the grounded permitted observations written,
    ``field_observation_ids`` their observation ids, ``evidence_id`` the content evidence they cite,
    ``dropped`` the per-observation verdicts the grounding gate rejected (each with its reason) and
    ``outcome`` the DCA-132 boundary verdict the gateway returned.
    """

    business_id: str
    subject_id: str
    source_key: str
    source_version: int
    content_ref: str
    authorized: bool = False
    refused_reason: str | None = None
    skipped: bool = False
    evidence_id: str | None = None
    recorded: tuple[ExtractedObservation, ...] = ()
    field_observation_ids: tuple[str, ...] = ()
    dropped: tuple[FactVerdict, ...] = ()
    outcome: UntrustedOutcome | None = None
    permitted_fields: tuple[str, ...] = ()
    overall_confidence: float | None = None
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    synthetic: bool = False

    @property
    def fields_recorded(self) -> int:
        return len(self.field_observation_ids)

    @property
    def produced(self) -> bool:
        return self.authorized and not self.skipped and self.refused_reason is None


def _digest(text: str) -> str:
    """A non-reversible SHA-256 (hex) of the extracted content — ties the run to the exact content."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _cited_facts(structured_output: dict | None) -> list[CitedFact]:
    """Map the gateway's ``observations`` output into DCA-133 :class:`CitedFact` objects (no mutation)."""
    rows = (structured_output or {}).get("observations") or []
    facts: list[CitedFact] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        facts.append(
            CitedFact(
                field_key=str(row.get("field") or ""),
                field_value=None if value is None else str(value),
                citation_ref=str(row.get("citationRef") or ""),
                cited_text=str(row.get("citedText") or ""),
                inference_label=str(row.get("inferenceLabel") or "observed"),
            )
        )
    return facts


class UnstructuredObservationExtractor:
    """AI-extract permitted organization observations from approved content, for ONE business.

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-048 rights gate (resolve permitted fields
    FIRST) with the accepted AI seam (DCA-128/131/132/133) and the DCA-061/062 writers; it writes only
    grounded organization observations and their provenance, never a contact.
    """

    capability = CAPABILITY

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._observations = OrgObservationStore(session, business_id)
        self._fields = FieldObservationStore(session, business_id)
        self._evidence = EvidenceObjectStore(session, business_id)
        self._preflight = SourcePreflight(session, business_id)
        self._catalog = SourceCatalogStore(session)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def extraction_for(
        self, source_key: str, subject_id: str, content_ref: str
    ) -> UnstructuredObservationExtraction | None:
        """The extraction-provenance row for one content piece + subject in this business, or ``None``."""
        stmt = (
            select(UnstructuredObservationExtraction)
            .where(UnstructuredObservationExtraction.business_id == self._business_id)
            .where(UnstructuredObservationExtraction.source_key == (source_key or "").strip())
            .where(UnstructuredObservationExtraction.subject_id == (subject_id or "").strip())
            .where(UnstructuredObservationExtraction.content_ref == (content_ref or "").strip())
        )
        return self._session.scalars(stmt).one_or_none()

    def extractions_for(
        self, source_key: str, subject_id: str
    ) -> list[UnstructuredObservationExtraction]:
        """Every extraction-provenance row recorded for one subject + source in this business."""
        stmt = (
            select(UnstructuredObservationExtraction)
            .where(UnstructuredObservationExtraction.business_id == self._business_id)
            .where(UnstructuredObservationExtraction.source_key == (source_key or "").strip())
            .where(UnstructuredObservationExtraction.subject_id == (subject_id or "").strip())
            .order_by(
                UnstructuredObservationExtraction.created_at,
                UnstructuredObservationExtraction.id,
            )
        )
        return list(self._session.scalars(stmt).all())

    def permitted_fields(
        self, source_key: str, purpose: str, *, now: datetime | None = None
    ) -> list[str]:
        """The fields this business may COLLECT from ``source_key`` for ``purpose``, right now.

        A pure, side-effect-free read: for every catalogued field of the source, a field is permitted iff
        the sanctioned DCA-048 :class:`~app.services.source_preflight.SourcePreflight` gate authorizes
        ``COLLECT`` on it (source approved+ENABLED, platform-approved, and the business entitlement grants
        it). A source with no available field yields an empty list — the extraction may collect nothing.
        """
        source = self._catalog.get((source_key or "").strip())
        if source is None:
            return []
        permitted: list[str] = []
        for field_name in source.fields or []:
            decision = self._preflight.authorize(
                PreflightRequest(
                    source_key=source.key,
                    operation=EXTRACT_OPERATION,
                    field=str(field_name),
                    purpose=purpose,
                ),
                now=now,
            )
            if decision.allowed:
                permitted.append(str(field_name))
        return permitted

    # -- the one extraction path --------------------------------------------------------------------

    def extract(
        self,
        facade: AiCapabilityFacade | None,
        request: UnstructuredExtractRequest,
        content: FetchedContent,
        actor: AuditActor,
        *,
        now: datetime | None = None,
        correlation_id: str | None = None,
    ) -> ExtractResult:
        """Authorize, AI-extract, re-ground and record the permitted organization observations of ``content``.

        Order (each stage writes nothing on failure): capability/transport gate (disabled → refusal,
        stages nothing, NO model call) → request-shape validation (raises) → subject must exist in THIS
        business (raises) → idempotency skip (an already-extracted content is a no-op, no model call) →
        rights gate (resolve permitted fields; an unentitled source → refusal, NO model call) → the ONE
        facade call keeping the body on the untrusted channel (DCA-132) → honour the gateway boundary
        (rejected → stage nothing) → independent citation grounding (DCA-133) → capture the content
        evidence (DCA-062) + record each grounded observation as a provenanced field observation (DCA-061)
        citing that evidence + stage the extraction-provenance row. Nothing is connected to or fetched.
        """
        moment = now or datetime.now(UTC)
        source_key = (request.source_key or "").strip()
        subject_id = (request.subject_observation_id or "").strip()
        content_ref = (content.content_ref or "").strip()

        def refusal(reason: str) -> ExtractResult:
            return ExtractResult(
                business_id=self._business_id,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                content_ref=content_ref,
                authorized=False,
                refused_reason=reason,
            )

        # 1) Capability/transport gate (DCA-128, fail-closed). A disabled capability stages nothing and
        #    never calls the model.
        if facade is None or not facade.can_invoke(CAPABILITY):
            return refusal(REASON_CAPABILITY_DISABLED)

        # 2) Request shape — a source + version, a purpose, an invocation key and a content ref + body.
        if not source_key:
            raise UnstructuredExtractError(REASON_MISSING_SOURCE_KEY, "a source_key is required")
        if request.source_version is None:
            raise UnstructuredExtractError(
                REASON_MISSING_SOURCE_VERSION, "a source_version is required"
            )
        purpose = (request.purpose or "").strip()
        if not purpose:
            raise UnstructuredExtractError(REASON_MISSING_PURPOSE, "a purpose is required")
        if not (request.invocation_key or "").strip():
            raise UnstructuredExtractError(
                REASON_MISSING_INVOCATION_KEY,
                "an invocation_key is required (idempotency/spend id)",
            )
        if not content_ref:
            raise UnstructuredExtractError(REASON_MISSING_CONTENT_REF, "a content_ref is required")
        if not (content.body or "").strip():
            raise UnstructuredExtractError(
                REASON_EMPTY_CONTENT, "the content body is empty; nothing can be extracted"
            )

        # 3) Subject — the observations must attach to an organization observation IN THIS BUSINESS. An
        #    unknown id, or one owned by another business, reads as not-found (never a cross-tenant link).
        if not subject_id or self._observations.get(subject_id) is None:
            raise UnstructuredExtractError(
                REASON_SUBJECT_NOT_FOUND,
                f"organization observation '{subject_id}' does not exist in this business",
                detail=subject_id,
            )

        # 4) Idempotency — a content piece already extracted for this subject is a deterministic no-op.
        #    Checked BEFORE authorizing/invoking, so a replay spends nothing and writes nothing.
        existing = self.extraction_for(source_key, subject_id, content_ref)
        if existing is not None:
            return ExtractResult(
                business_id=self._business_id,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                content_ref=content_ref,
                authorized=True,
                skipped=True,
                evidence_id=existing.evidence_id,
            )

        # 5) Rights FIRST (DCA-048). Resolve the fields this business may COLLECT from the source. No
        #    permitted field → the source is not approved+entitled: refuse, stage nothing, NEVER call the
        #    model (no egress, no spend).
        permitted = self.permitted_fields(source_key, purpose, now=now)
        if not permitted:
            return refusal(REASON_SOURCE_NOT_ENTITLED)

        # 6) Build the request keeping the content BODY on the untrusted channel (DCA-132), then run the
        #    ONE facade call (DCA-131 minimises the trusted context). A contract violation is a
        #    fail-closed refusal (no state change).
        trusted_context: dict = {
            "subjectType": FieldSubjectType.ORGANIZATION.value,
            "permittedFields": sorted(permitted),
            "evidenceRefs": [content_ref],
        }
        request_envelope = build_untrusted_request(
            CAPABILITY,
            self._business_id,
            trusted_context=trusted_context,
            external=[ExternalContent(provenance=content.label(), content=content.body)],
            correlation_id=correlation_id,
        )
        binding = dca_job_binding(request.invocation_key.strip(), self._business_id)
        try:
            result = facade.invoke(request_envelope, binding)
        except (AiCapabilityDisabled, AiContractViolation):
            return refusal(REASON_CAPABILITY_DISABLED)

        # 7) Honour the gateway's returned safety signals (DCA-132). A rejected output is
        #    unsafe/unusable — store NOTHING.
        outcome = evaluate_untrusted_result(result)
        if outcome.rejected:
            res = refusal(REASON_OUTPUT_REJECTED)
            res.outcome = outcome
            res.model_provider = result.model_provider
            res.model_id = result.model_id
            res.prompt_version = result.prompt_version
            res.synthetic = result.synthetic
            return res

        # 8) Independent citation grounding (DCA-133): the gateway's 'valid' does NOT authorize a write.
        #    Each observation must cite a span found in the content the DCA retrieved AND name a permitted
        #    field — a non-permitted (incl. personal/contact) field is dropped as unrelated.
        source = CitedSource(
            ref=content_ref,
            content=content.body,
            permitted_fields=frozenset(permitted),
            rights_valid=True,
        )
        validation = validate_citations(_cited_facts(result.structured_output), [source])
        grounded = validation.accepted
        dropped = validation.rejected

        rights_basis = (
            request.rights_basis or ""
        ).strip() or f"approved_source:{source_key}/collect/{purpose}"

        # 9) Capture the content as a license-aware evidence object (DCA-062) — the grounding each
        #    observation cites.
        evidence = self._evidence.store(
            EvidenceInput(
                source_key=source_key,
                source_version=request.source_version,
                content_kind=(content.content_kind or DEFAULT_CONTENT_KIND),
                content_ref=content_ref,
                raw_content=content.body,
                captured_at=moment,
                rights_basis=rights_basis,
                content_media_type=(content.content_media_type or "").strip(),
                subject_type=FieldSubjectType.ORGANIZATION.value,
                subject_id=subject_id,
            ),
            actor=actor,
            now=moment,
        )

        # 10) Record each grounded observation as a provenanced field observation (DCA-061) that CITES the
        #     content evidence (the provenance_ref grounds the observation in the captured content).
        provenance_ref = f"{content_ref}#evidence={evidence.id}"
        recorded: list[ExtractedObservation] = []
        field_observation_ids: list[str] = []
        for fact in grounded:
            value = (fact.field_value or "").strip()
            observation = self._fields.record(
                FieldObservationInput(
                    subject_type=FieldSubjectType.ORGANIZATION.value,
                    subject_id=subject_id,
                    field_key=fact.field_key,
                    field_value=value,
                    inference_label=fact.inference_label,
                    provenance_ref=provenance_ref,
                    observed_at=moment,
                    scope=request.scope,
                    rights_basis=rights_basis,
                    source_key=source_key,
                    source_version=request.source_version,
                ),
                actor=actor,
                now=moment,
            )
            field_observation_ids.append(observation.id)
            recorded.append(
                ExtractedObservation(
                    field_key=fact.field_key,
                    field_value=value,
                    inference_label=fact.inference_label,
                )
            )

        # 11) Stage the run-level extraction provenance (the idempotency key + the citation to evidence).
        structured = result.structured_output or {}
        self._session.add(
            UnstructuredObservationExtraction(
                id=str(uuid.uuid4()),
                business_id=self._business_id,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                content_ref=content_ref,
                evidence_id=evidence.id,
                content_digest=_digest(content.body),
                fields_recorded=len(field_observation_ids),
                observations_dropped=len(dropped),
                prompt_version=str(result.prompt_version or ""),
                synthetic=bool(result.synthetic),
                schema_version=EXTRACT_SCHEMA_VERSION,
                created_at=moment,
            )
        )

        return ExtractResult(
            business_id=self._business_id,
            subject_id=subject_id,
            source_key=source_key,
            source_version=request.source_version,
            content_ref=content_ref,
            authorized=True,
            evidence_id=evidence.id,
            recorded=tuple(recorded),
            field_observation_ids=tuple(field_observation_ids),
            dropped=dropped,
            outcome=outcome,
            permitted_fields=tuple(sorted(permitted)),
            overall_confidence=structured.get("confidence"),
            model_provider=result.model_provider,
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            synthetic=result.synthetic,
        )


__all__ = [
    "CAPABILITY",
    "DEFAULT_CONTENT_KIND",
    "EXTRACT_OPERATION",
    "REASON_CAPABILITY_DISABLED",
    "REASON_OUTPUT_REJECTED",
    "REASON_SOURCE_NOT_ENTITLED",
    "REASON_MISSING_SOURCE_KEY",
    "REASON_MISSING_SOURCE_VERSION",
    "REASON_MISSING_PURPOSE",
    "REASON_MISSING_INVOCATION_KEY",
    "REASON_MISSING_CONTENT_REF",
    "REASON_EMPTY_CONTENT",
    "REASON_SUBJECT_NOT_FOUND",
    "FetchedContent",
    "UnstructuredExtractRequest",
    "ExtractedObservation",
    "ExtractResult",
    "UnstructuredExtractError",
    "UnstructuredObservationExtractor",
]
