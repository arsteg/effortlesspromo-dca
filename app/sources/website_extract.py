"""DCA-124 — extract facts from an approved business website (the second concrete source adapter).

Key: ``web-org-adapter`` · source sections 6, 8, 9. Stage: Approved source access.

The first concrete adapter (DCA-123 :mod:`app.sources.org_dataset_import`) loaded an approved dataset;
this is an ORGANIZATION-ONLY *website* adapter that, for an organization observation the business is
already looking at, DETERMINISTICALLY parses a bounded set of permitted COMPANY fields out of an
APPROVED + entitled business web page and records them as provenanced observations. It builds on, and
never bypasses, the already-built chain:

- the GUARDED FETCHER (DCA-118..121) — authorization goes through
  :class:`~app.sources.fetch_policy.FetchPolicyGate`, which checks the DCA-044..048 source-rights chain
  FIRST and only then the per-host crawl limits (robots path / crawl-delay / rate / concurrency). A
  rights denial writes nothing and never touches the crawl limiter; a robots-disallowed path is a hard
  crawl denial — so the adapter can neither read a source it is not entitled for NOR wander onto an
  unrestricted profile path the site disallows;
- the DCA-122 source-adapter capability contract — the adapter carries an ORGANIZATION-ONLY capability
  descriptor (:data:`WEBSITE_EXTRACT_CAPABILITY`), so it declares no personal-contact operation and can
  never write a contact field;
- the DCA-061 :class:`~app.services.field_observation.FieldObservationStore` (each extracted company
  field becomes a provenanced observation) and the DCA-062
  :class:`~app.services.evidence_store.EvidenceObjectStore` (the page itself becomes a bounded,
  license-aware evidence object the facts CITE) — the two sanctioned writers, never bypassed.

There is NO network fetch here — fetching stays a disabled future capability. The page content is
CALLER-SUPPLIED / synthetic and is treated as UNTRUSTED input: it is only scanned (a bounded,
regex-based read that parses, decodes-as-code, renders or evaluates NOTHING), its company facts are
read off an explicit ALLOWLIST, and any email/phone fragment it carries is detected and DROPPED, never
recorded. **AI extraction is explicitly a later story — this adapter uses no model at all**, so it
makes no AI-gateway call and spends nothing.

The load-bearing guarantees (the DCA-124 acceptance criteria):

- **Only permitted company fields are collected; no unrelated contact field leaks.** Extraction reads
  only the :data:`PERMITTED_ORG_FIELDS` allowlist (all organization-level), and the personal-contact
  detector proves any contact fragment on the page is dropped (counted, never written). The adapter is
  organization-only (DCA-122), so there is no declared or executed path to a contact field.
- **It cannot crawl unrestricted profiles.** A page can only be extracted for an organization
  observation the business already holds, from a source it is approved+entitled for, on a path the
  site's robots policy allows — a rights denial OR a robots-disallowed path extracts NOTHING.
- **Reproduce the result and the denial without unintended state changes.** A denial (rights or crawl)
  writes no evidence, no field observation and no provenance row; a re-extraction of the same page is a
  deterministic no-op (the ``(source, subject, page)`` row is found and skipped — no new crawl slot, no
  duplicate facts).

The adapter stages into the caller's session and never commits — the caller owns the unit of work, so
the evidence, the field observations, their atomic audit and the extraction-provenance row all commit
together.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor
from app.db.models import (
    FieldInferenceLabel,
    FieldSubjectType,
    SourceAccessMethod,
    SourceKind,
    SourceOperation,
    WebsiteFactExtraction,
)
from app.services.evidence_store import EvidenceInput, EvidenceObjectStore
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import OrgObservationStore
from app.services.source_preflight import PreflightRequest
from app.sources.capabilities import (
    AdapterOperation,
    SourceAdapterCapability,
    validate_capability,
)
from app.sources.crawl_limits import CrawlPolicy, HostCrawlLimiter
from app.sources.fetch_policy import FetchPolicyGate, FetchPolicyRequest, FetchPhase

# The adapter key (distinct from a catalog Source key — the website's source is named in the request).
ADAPTER_KEY = "dca.website_extract"

# The ORGANIZATION-ONLY capability this adapter declares (DCA-122). It enriches organization-level
# fields for a KNOWN organization; it touches NO personal contact data, so ``handles_personal_contacts``
# is False and it declares no ``ENRICH_CONTACT`` operation — the structural guarantee that this adapter
# can never read or write a contact field. Validated at import time so an incoherent descriptor fails
# loudly on load, never silently.
WEBSITE_EXTRACT_CAPABILITY: SourceAdapterCapability = validate_capability(
    SourceAdapterCapability(
        key=ADAPTER_KEY,
        source_kind=SourceKind.COMMERCIAL,
        access_method=SourceAccessMethod.PORTAL_LOOKUP,
        operations=frozenset(
            {
                AdapterOperation.LIST_CAPABILITIES,
                AdapterOperation.ENRICH_ORGANIZATION,
            }
        ),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=False,
        trust_tier="business_website",
        notes="DCA-124 approved business-website fact extractor (organization-only, deterministic, no fetch)",
    )
)

# The source RIGHT (DCA-045 per-field operation) an extraction depends on, and the version of the
# extraction contract provenance rows are written under (expand→migrate→contract).
EXTRACT_OPERATION = SourceOperation.COLLECT.value
EXTRACT_SCHEMA_VERSION = 1

# The content-kind recorded for the captured page evidence object (DCA-062).
EVIDENCE_CONTENT_KIND = "business_website"

# A conservative scan bound: at most this many characters of the (untrusted, caller-supplied) page body
# are scanned for facts. Larger bodies are truncated for scanning — the adapter never buffers an
# unbounded untrusted string into the matcher.
MAX_SCAN_CHARS = 200_000

# The ALLOWLIST of ORGANIZATION-level fields this adapter may collect. Every key is a company fact; the
# list contains NO contact channel, so the adapter has no permitted field that could carry personal
# contact data. A field not on this list is never recorded, regardless of what the page contains.
PERMITTED_ORG_FIELDS: frozenset[str] = frozenset(
    {"legal_name", "description", "industry", "cin", "gstin"}
)

# Deterministic, ALLOWLIST-keyed extractors. Each entry is (field_key, compiled pattern) and yields at
# most one value per field (the FIRST matching extractor for a key wins; later ones are ignored). The
# patterns match only organization facts — never an email/phone — and the body is only scanned, never
# executed. Order defines priority.
_EXTRACTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "legal_name",
        re.compile(
            r"""<meta[^>]+?(?:property|name)=["']og:site_name["'][^>]+?"""
            r"""content=["']([^"']{1,300})["']""",
            re.IGNORECASE,
        ),
    ),
    ("legal_name", re.compile(r"""(?mi)^\s*legal\s*name\s*[:=]\s*(.+?)\s*$""")),
    (
        "description",
        re.compile(
            r"""<meta[^>]+?name=["']description["'][^>]+?content=["']([^"']{1,500})["']""",
            re.IGNORECASE,
        ),
    ),
    ("industry", re.compile(r"""(?mi)^\s*industry\s*[:=]\s*(.+?)\s*$""")),
    (
        "cin",
        re.compile(r"""(?i)\bcin\s*[:=]?\s*([A-Za-z]\d{5}[A-Za-z]{2}\d{4}[A-Za-z]{3}\d{6})\b"""),
    ),
    (
        "gstin",
        re.compile(r"""(?i)\bgstin\s*[:=]?\s*(\d{2}[A-Za-z]{5}\d{4}[A-Za-z]\d[Zz][A-Za-z\d])\b"""),
    ),
)

# Personal/contact fragments the adapter explicitly DETECTS and DROPS (counted, never recorded). These
# are NOT extractors — they exist so an email/phone the page carries is proven to leak nowhere.
_CONTACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"""),  # email address
    re.compile(r"""(?<!\d)(?:\+91[\s\-]?)?[6-9]\d{9}(?!\d)"""),  # Indian mobile number
)

# Request-shape / schema refusals — each raised BEFORE the session is mutated, so a refusal writes nothing.
REASON_MISSING_SOURCE_KEY = "request_missing_source_key"
REASON_MISSING_SOURCE_VERSION = "request_missing_source_version"
REASON_MISSING_PURPOSE = "request_missing_purpose"
REASON_MISSING_URL = "request_missing_url"
REASON_UNPARSEABLE_URL = "request_unparseable_url"
REASON_EMPTY_PAGE = "page_body_empty"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"


class WebsiteExtractError(Exception):
    """A request/schema problem refused the extraction with a stable code. Raised before the session is
    mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WebsitePage:
    """A caller-supplied / synthetic page the adapter extracts from — never a live socket.

    ``url`` is the page locator (its host is matched against the crawl limits, its path against robots,
    and the whole URL is the checkpointable ``page_ref``). ``content_type`` is the declared type (e.g.
    ``text/html``) and ``body`` is the UNTRUSTED page content the adapter scans. The adapter resolves /
    fetches nothing — the body stands in for what a (still-disabled) fetch would return.
    """

    url: str
    content_type: str
    body: str


@dataclass(frozen=True)
class WebsiteExtractRequest:
    """One would-be website extraction for an organization observation the business already holds.

    ``subject_observation_id`` is the :class:`~app.db.models.OrgObservation` (in THIS business) the
    facts are about. ``source_key`` / ``source_version`` name the approved+entitled catalog source and
    its terms version the page is read under; ``purpose`` the purpose the extraction serves (rights are
    purpose-bound). ``scope`` is the India-only collection scope recorded on each fact (``nationwide``
    or a recognized state/UT). ``rights_basis`` is the recorded rights basis (derived from the purpose
    when left blank). ``policy`` is the host's crawl policy (robots Allow/Disallow + rate caps), supplied
    by the caller exactly as the guarded fetcher takes a caller-supplied policy.
    """

    subject_observation_id: str
    source_key: str
    source_version: int
    purpose: str
    scope: str = "nationwide"
    rights_basis: str = ""
    policy: CrawlPolicy = dc_field(default_factory=CrawlPolicy)


@dataclass(frozen=True)
class ExtractedFact:
    """One permitted organization fact read off the page: a field key and its value."""

    field_key: str
    field_value: str


@dataclass(frozen=True)
class ExtractResult:
    """The deterministic outcome of one extraction run (nothing fetched; inert on a denial).

    ``authorized`` is the guarded-fetcher verdict; on a denial ``phase`` is which phase decided it
    (``rights``/``crawl``), ``reason`` the stable denial reason, and everything else is empty (nothing
    was written). On success ``skipped`` marks the idempotent no-op (the page was already extracted for
    this subject): ``extracted`` / ``field_observation_ids`` are then empty and ``evidence_id`` points
    at the existing evidence. On a fresh success ``extracted`` are the permitted company facts recorded,
    ``field_observation_ids`` their observation ids, ``evidence_id`` the page evidence the facts cite,
    and ``contact_fields_dropped`` how many personal/contact fragments were detected and DROPPED.
    """

    authorized: bool
    subject_id: str
    source_key: str
    source_version: int
    page_ref: str
    host: str = ""
    phase: str = ""
    reason: str | None = None
    skipped: bool = False
    evidence_id: str | None = None
    extracted: tuple[ExtractedFact, ...] = ()
    field_observation_ids: tuple[str, ...] = ()
    contact_fields_dropped: int = 0

    @property
    def fields_recorded(self) -> int:
        return len(self.field_observation_ids)


def _digest(text: str) -> str:
    """A non-reversible SHA-256 (hex) of the scanned page bytes — ties the run to the exact content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_org_facts(body: str) -> tuple[list[ExtractedFact], int]:
    """Deterministically read permitted ORGANIZATION facts off ``body`` — pure, network-free, no execution.

    Returns ``(facts, contact_fields_dropped)``. ``facts`` carries at most one value per
    :data:`PERMITTED_ORG_FIELDS` key (first matching extractor wins, keys in a stable order);
    ``contact_fields_dropped`` is how many email/phone fragments were DETECTED — proof the adapter sees
    them and records NONE. The body is only scanned (bounded to :data:`MAX_SCAN_CHARS`); nothing in it
    is parsed-as-code, decoded, rendered or evaluated.
    """
    scanned = (body or "")[:MAX_SCAN_CHARS]

    found: dict[str, str] = {}
    for field_key, pattern in _EXTRACTORS:
        if field_key in found:
            continue  # first matching extractor for a key wins (stable priority)
        match = pattern.search(scanned)
        if match is None:
            continue
        value = match.group(1).strip()
        if value and field_key in PERMITTED_ORG_FIELDS:
            found[field_key] = value

    # Stable output order: by the allowlist encounter order in _EXTRACTORS.
    ordered: list[ExtractedFact] = []
    seen: set[str] = set()
    for field_key, _ in _EXTRACTORS:
        if field_key in found and field_key not in seen:
            ordered.append(ExtractedFact(field_key, found[field_key]))
            seen.add(field_key)

    contact_hits = sum(len(p.findall(scanned)) for p in _CONTACT_PATTERNS)
    return ordered, contact_hits


class WebsiteFactExtractor:
    """Extract permitted organization facts from an approved business website, for ONE business.

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-121 guarded fetcher (rights FIRST, then
    crawl limits) with the DCA-061 field-observation store and the DCA-062 evidence store; it writes
    only organization facts and their provenance, never a contact.
    """

    capability: SourceAdapterCapability = WEBSITE_EXTRACT_CAPABILITY

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._gate = FetchPolicyGate(session, business_id)
        self._limiter = HostCrawlLimiter(session, business_id)
        self._observations = OrgObservationStore(session, business_id)
        self._fields = FieldObservationStore(session, business_id)
        self._evidence = EvidenceObjectStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def extraction_for(
        self, source_key: str, subject_id: str, page_ref: str
    ) -> WebsiteFactExtraction | None:
        """The extraction-provenance row for one page + subject in this business, or ``None``."""
        stmt = (
            select(WebsiteFactExtraction)
            .where(WebsiteFactExtraction.business_id == self._business_id)
            .where(WebsiteFactExtraction.source_key == (source_key or "").strip())
            .where(WebsiteFactExtraction.subject_id == (subject_id or "").strip())
            .where(WebsiteFactExtraction.page_ref == (page_ref or "").strip())
        )
        return self._session.scalars(stmt).one_or_none()

    def extractions_for(self, source_key: str, subject_id: str) -> list[WebsiteFactExtraction]:
        """Every extraction-provenance row recorded for one subject + source in this business."""
        stmt = (
            select(WebsiteFactExtraction)
            .where(WebsiteFactExtraction.business_id == self._business_id)
            .where(WebsiteFactExtraction.source_key == (source_key or "").strip())
            .where(WebsiteFactExtraction.subject_id == (subject_id or "").strip())
            .order_by(WebsiteFactExtraction.created_at, WebsiteFactExtraction.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one extraction path --------------------------------------------------------------------

    def extract(
        self,
        request: WebsiteExtractRequest,
        page: WebsitePage,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> ExtractResult:
        """Authorize, deterministically parse, and record the permitted organization facts of ``page``.

        Order (each stage writes nothing on failure): request-shape validation (raises
        :class:`WebsiteExtractError`) → subject must exist in THIS business (raises) → idempotency skip
        (an already-extracted page is a no-op) → the DCA-121 guarded fetcher (rights FIRST, then crawl
        limits; a denial writes nothing) → deterministic allowlist parse → capture the page evidence
        (DCA-062) + record each permitted fact as a provenanced observation (DCA-061) that cites that
        evidence + stage the extraction-provenance row. Nothing is connected to or fetched, and no model
        is called.
        """
        moment = now or datetime.now(UTC)

        # 1) Request shape — a source + version, a purpose, and a page URL are all required.
        source_key = (request.source_key or "").strip()
        if not source_key:
            raise WebsiteExtractError(REASON_MISSING_SOURCE_KEY, "a source_key is required")
        if request.source_version is None:
            raise WebsiteExtractError(REASON_MISSING_SOURCE_VERSION, "a source_version is required")
        purpose = (request.purpose or "").strip()
        if not purpose:
            raise WebsiteExtractError(REASON_MISSING_PURPOSE, "a purpose is required")
        page_ref = (page.url or "").strip()
        if not page_ref:
            raise WebsiteExtractError(REASON_MISSING_URL, "a page url is required")
        host = _host_of(page_ref)
        if not host:
            raise WebsiteExtractError(
                REASON_UNPARSEABLE_URL, f"page url '{page_ref}' has no host", detail=page_ref
            )
        if not (page.body or "").strip():
            raise WebsiteExtractError(
                REASON_EMPTY_PAGE, "the page body is empty; nothing can be extracted"
            )

        # 2) Subject — the facts must attach to an organization observation IN THIS BUSINESS. An unknown
        #    id, or one owned by another business, reads as not-found (never a cross-tenant link).
        subject_id = (request.subject_observation_id or "").strip()
        if not subject_id or self._observations.get(subject_id) is None:
            raise WebsiteExtractError(
                REASON_SUBJECT_NOT_FOUND,
                f"organization observation '{subject_id}' does not exist in this business",
                detail=subject_id,
            )

        # 3) Idempotency — a page already extracted for this subject is a deterministic no-op. Checked
        #    BEFORE authorizing, so a replay reserves no crawl slot and writes nothing.
        existing = self.extraction_for(source_key, subject_id, page_ref)
        if existing is not None:
            return ExtractResult(
                authorized=True,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                page_ref=page_ref,
                host=host,
                skipped=True,
                evidence_id=existing.evidence_id,
                contact_fields_dropped=existing.contact_fields_dropped,
            )

        # 4) Guarded fetcher (DCA-121) — rights FIRST, then crawl limits. A denial writes NOTHING: a
        #    rights denial never touches the crawl limiter; a robots-disallowed path is a hard crawl deny.
        decision = self._gate.authorize_fetch(
            FetchPolicyRequest(
                preflight=PreflightRequest(
                    source_key=source_key,
                    operation=EXTRACT_OPERATION,
                    purpose=purpose,
                    field="",
                ),
                url=page_ref,
                policy=request.policy,
            ),
            now=now,
        )
        if not decision.allowed:
            return ExtractResult(
                authorized=False,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                page_ref=page_ref,
                host=host,
                phase=decision.phase.value,
                reason=decision.reason,
            )
        # The synthetic page is already in hand, so the reserved request is immediately "complete":
        # release the in-flight concurrency slot the guarded fetcher took (the rate-window count stands,
        # since a request against the host WAS made) so concurrency accounting does not leak.
        if decision.phase is FetchPhase.CRAWL and decision.reservation is not None:
            self._limiter.release(source_key, host, now=now)

        # 5) Deterministic allowlist parse of the UNTRUSTED body — only company facts, contacts dropped.
        facts, contact_dropped = extract_org_facts(page.body)
        content_digest = _digest((page.body or "")[:MAX_SCAN_CHARS])

        # 6) Capture the page as a license-aware evidence object (DCA-062) — the grounding each fact cites.
        rights_basis = (
            request.rights_basis or ""
        ).strip() or f"approved_source:{source_key}/collect/{purpose}"
        evidence = self._evidence.store(
            EvidenceInput(
                source_key=source_key,
                source_version=request.source_version,
                content_kind=EVIDENCE_CONTENT_KIND,
                content_ref=page_ref,
                raw_content=page.body,
                captured_at=moment,
                rights_basis=rights_basis,
                content_media_type=(page.content_type or "").strip(),
                subject_type=FieldSubjectType.ORGANIZATION.value,
                subject_id=subject_id,
            ),
            actor=actor,
            now=moment,
        )

        # 7) Record each permitted company fact as a provenanced observation (DCA-061) that CITES the
        #    page evidence (the provenance_ref grounds the fact in the captured page).
        provenance_ref = f"{page_ref}#evidence={evidence.id}"
        field_observation_ids: list[str] = []
        for fact in facts:
            observation = self._fields.record(
                FieldObservationInput(
                    subject_type=FieldSubjectType.ORGANIZATION.value,
                    subject_id=subject_id,
                    field_key=fact.field_key,
                    field_value=fact.field_value,
                    inference_label=FieldInferenceLabel.OBSERVED.value,
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

        # 8) Stage the run-level extraction provenance (the idempotency key + the citation to evidence).
        self._session.add(
            WebsiteFactExtraction(
                id=str(uuid.uuid4()),
                business_id=self._business_id,
                subject_id=subject_id,
                source_key=source_key,
                source_version=request.source_version,
                page_ref=page_ref,
                host=host,
                evidence_id=evidence.id,
                content_digest=content_digest,
                fields_recorded=len(field_observation_ids),
                contact_fields_dropped=contact_dropped,
                schema_version=EXTRACT_SCHEMA_VERSION,
                created_at=moment,
            )
        )

        return ExtractResult(
            authorized=True,
            subject_id=subject_id,
            source_key=source_key,
            source_version=request.source_version,
            page_ref=page_ref,
            host=host,
            phase=decision.phase.value,
            evidence_id=evidence.id,
            extracted=tuple(facts),
            field_observation_ids=tuple(field_observation_ids),
            contact_fields_dropped=contact_dropped,
        )


def _host_of(url: str) -> str:
    """The lower-cased host of ``url`` (trailing dot stripped), or ``""`` if unparseable/hostless."""
    try:
        host = urlsplit((url or "").strip()).hostname
    except ValueError:
        return ""
    return (host or "").lower().rstrip(".")


__all__ = [
    "ADAPTER_KEY",
    "WEBSITE_EXTRACT_CAPABILITY",
    "PERMITTED_ORG_FIELDS",
    "EVIDENCE_CONTENT_KIND",
    "WebsitePage",
    "WebsiteExtractRequest",
    "ExtractedFact",
    "ExtractResult",
    "WebsiteExtractError",
    "WebsiteFactExtractor",
    "extract_org_facts",
    "REASON_MISSING_SOURCE_KEY",
    "REASON_MISSING_SOURCE_VERSION",
    "REASON_MISSING_PURPOSE",
    "REASON_MISSING_URL",
    "REASON_UNPARSEABLE_URL",
    "REASON_EMPTY_PAGE",
    "REASON_SUBJECT_NOT_FOUND",
]
