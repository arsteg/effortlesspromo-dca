"""DCA-123 — import an approved organization dataset (the first concrete source adapter).

Key: ``dataset-import`` · source sections 6, 8, 9. Stage: Approved source access.

This is the FIRST concrete source adapter: a manifest-driven importer that loads an APPROVED + entitled
ORGANIZATION dataset into the DCA org-observation store (DCA-059), with durable source provenance. It
builds on, and never bypasses, the already-built chain:

- the source-rights gate (DCA-048 :class:`~app.services.source_preflight.SourcePreflight`, which
  DCA-121 composes) — the import proceeds only when the source is approved AND the business is entitled
  to ``collect`` for the manifest's purpose; a denial imports NOTHING and changes no state;
- the DCA-122 source-adapter capability contract — the importer carries an ORGANIZATION-ONLY capability
  descriptor (:data:`ORG_DATASET_IMPORT_CAPABILITY`), so it can never declare or write a personal
  contact field; a dataset row carrying personal contact fields is REFUSED, never partially imported;
- the DCA-059 :class:`~app.services.org_observation.OrgObservationStore` — the single sanctioned writer
  of org observations and their external-id mappings; the importer records identities through it (and
  its atomic audit), adding only the import provenance alongside.

There is NO network fetch here — fetching stays a disabled future capability. The importer takes a
caller-supplied / synthetic :class:`DatasetManifest` + :class:`DatasetRow` list (an already-approved
dataset) and writes it deterministically. The load-bearing guarantees (the DCA-123 acceptance criteria):

- **An unapproved manifest is rejected and imports nothing.** An incomplete manifest (missing dataset
  id / source / version / license / purpose) raises :class:`OrgDatasetImportError` before any state is
  touched; a manifest whose source is not approved+entitled returns an un-authorized
  :class:`ImportResult` (the preflight denial) having written nothing.
- **Allowed rows retain source attribution and checkpointable row references.** Each imported
  observation gets an :class:`~app.db.models.OrgObservationImport` provenance row recording the source
  key, dataset id, source version, license and the dataset ``source_row_ref`` — the checkpointable
  reference a later refresh re-locates the row by.
- **A re-import does not duplicate.** ``(business_id, source_key, dataset_id, source_row_ref)`` is the
  idempotency key: a row already imported for this dataset is SKIPPED (its existing observation reused),
  so re-running the same import is a deterministic no-op.
- **Organization-only.** No personal contact field is ever imported; a row carrying one is refused.

The importer stages into the caller's session and never commits — the caller owns the unit of work, so
the observations, their mappings, their atomic audit and the provenance rows all commit together.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor
from app.db.models import OrgObservationImport, SourceAccessMethod, SourceKind, SourceOperation
from app.services.org_observation import (
    ExternalIdInput,
    ObservationInput,
    OrgObservationError,
    OrgObservationStore,
)
from app.services.source_preflight import PreflightRequest, SourcePreflight
from app.sources.capabilities import (
    AdapterOperation,
    SourceAdapterCapability,
    validate_capability,
)

# The adapter key (distinct from a catalog Source key — the dataset's source is named in the manifest).
ADAPTER_KEY = "dca.org_dataset_import"

# The ORGANIZATION-ONLY capability this adapter declares (DCA-122). It touches NO personal contact data,
# so ``handles_personal_contacts`` is False and it declares no ``ENRICH_CONTACT`` operation — the
# structural guarantee that this importer can never write a contact field. Validated at import time so
# an incoherent descriptor fails loudly on load, never silently.
ORG_DATASET_IMPORT_CAPABILITY: SourceAdapterCapability = validate_capability(
    SourceAdapterCapability(
        key=ADAPTER_KEY,
        source_kind=SourceKind.OFFICIAL,
        access_method=SourceAccessMethod.BULK_DOWNLOAD,
        operations=frozenset(
            {
                AdapterOperation.LIST_CAPABILITIES,
                AdapterOperation.ESTIMATE,
                AdapterOperation.DISCOVER_ORGANIZATIONS,
                AdapterOperation.ENRICH_ORGANIZATION,
            }
        ),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=False,
        trust_tier="official_registry",
        notes="DCA-123 approved organization dataset importer (organization-only, no fetch)",
    )
)

# The source RIGHT (DCA-045 per-field operation) an import depends on, and the version of the import
# contract provenance rows are written under (expand→migrate→contract).
IMPORT_OPERATION = SourceOperation.COLLECT.value
IMPORT_SCHEMA_VERSION = 1

# A conservative streaming bound: a single import may carry at most this many rows. A larger dataset is
# refused whole (import nothing) rather than loaded unbounded into memory.
DEFAULT_MAX_IMPORT_ROWS = 10_000

# Manifest / schema refusals — each raised BEFORE the session is mutated, so a refusal imports nothing.
REASON_MISSING_DATASET_ID = "manifest_missing_dataset_id"
REASON_MISSING_SOURCE_KEY = "manifest_missing_source_key"
REASON_MISSING_SOURCE_VERSION = "manifest_missing_source_version"
REASON_MISSING_LICENSE = "manifest_missing_license"
REASON_MISSING_PURPOSE = "manifest_missing_purpose"
REASON_DATASET_TOO_LARGE = "dataset_exceeds_row_bound"
REASON_ROW_MISSING_REF = "row_missing_source_ref"
REASON_DUPLICATE_ROW_REF = "duplicate_source_row_ref"

# Per-row refusal reasons (the row stages nothing; other rows still import).
REASON_PERSONAL_FIELDS = "personal_fields_not_importable"
REASON_PARENT_ROW_UNRESOLVED = "parent_row_ref_unresolved"


class OrgDatasetImportError(Exception):
    """A manifest/schema problem refused the import with a stable code. Raised before the session is
    mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DatasetManifest:
    """The manifest describing one approved organization dataset release (dataset/license/source version).

    ``dataset_id`` is the opaque id of the dataset release; ``source_key`` the catalog
    :class:`~app.db.models.Source` the dataset was collected under (the rights-gated key); ``purpose``
    the purpose the import serves (rights are purpose-bound). ``source_version`` is the dataset/source
    snapshot label and ``license`` its declared license — both recorded as source attribution on every
    imported row. All five are REQUIRED; an incomplete manifest is rejected (import nothing).
    """

    dataset_id: str
    source_key: str
    source_version: str
    license: str
    purpose: str
    schema_version: int = IMPORT_SCHEMA_VERSION


@dataclass(frozen=True)
class DatasetRow:
    """One organization row of an approved dataset (ORGANIZATION-ONLY).

    ``row_ref`` is the stable, checkpointable reference of this row within the dataset (the idempotency
    key and the handle a later refresh re-locates it by) — REQUIRED. ``entity_type`` is ``legal_entity``
    or ``establishment`` (:class:`~app.db.models.OrgObservationType`); ``parent_row_ref`` names (by
    dataset row ref) the parent legal entity of an establishment. ``external_ids`` are the organization
    register identifiers (CIN / PAN / GSTIN / …) to attach.

    ``contact`` is a guard, not a feature: this importer is organization-only, so a row that carries ANY
    personal contact field is REFUSED (never partially imported). It is present so a dataset mixing
    personal data is rejected explicitly rather than silently stripped.
    """

    row_ref: str
    entity_type: str
    display_name: str
    parent_row_ref: str = ""
    state_code: str = ""
    external_ids: tuple[ExternalIdInput, ...] = ()
    contact: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportedRow:
    """One dataset row's outcome: the checkpointable ref, the observation it maps to, and its type."""

    row_ref: str
    observation_id: str
    entity_type: str


@dataclass(frozen=True)
class ImportResult:
    """The deterministic outcome of one import run (nothing fetched; inert on a denial).

    ``authorized`` is the rights verdict; on a denial ``reason`` is the stable DCA-048 preflight reason
    and ``imported`` / ``skipped`` are empty (nothing was written). On success ``imported`` are the rows
    newly recorded this run, ``skipped`` the rows already imported for this dataset (the idempotent
    no-op), and ``refused_rows`` the ``(row_ref, reason)`` pairs refused per-row (e.g. personal fields),
    each of which staged nothing.
    """

    authorized: bool
    dataset_id: str
    source_key: str
    source_version: str
    reason: str | None = None
    imported: tuple[ImportedRow, ...] = ()
    skipped: tuple[ImportedRow, ...] = ()
    refused_rows: tuple[tuple[str, str], ...] = ()

    @property
    def imported_count(self) -> int:
        return len(self.imported)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _order_key(row: DatasetRow) -> int:
    """Process legal entities before establishments so an establishment's in-dataset parent exists."""
    et = (row.entity_type or "").strip().lower()
    if et == "legal_entity":
        return 0
    if et == "establishment":
        return 1
    return 2  # unknown types sort last; the store rejects them per-row


class OrgDatasetImporter:
    """Import an approved organization dataset into org observations, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-048 rights preflight (checked FIRST) with
    the DCA-059 observation store; it writes only organization data and its provenance, never a contact.
    """

    capability: SourceAdapterCapability = ORG_DATASET_IMPORT_CAPABILITY

    def __init__(
        self, session: Session, business_id: str, *, max_rows: int = DEFAULT_MAX_IMPORT_ROWS
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._max_rows = max_rows
        self._preflight = SourcePreflight(session, business_id)
        self._observations = OrgObservationStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def imports_for(self, dataset_id: str, source_key: str) -> list[OrgObservationImport]:
        """Every import-provenance row recorded for one dataset, in this business (oldest first)."""
        stmt = (
            select(OrgObservationImport)
            .where(OrgObservationImport.business_id == self._business_id)
            .where(OrgObservationImport.source_key == (source_key or "").strip())
            .where(OrgObservationImport.dataset_id == (dataset_id or "").strip())
            .order_by(OrgObservationImport.created_at, OrgObservationImport.id)
        )
        return list(self._session.scalars(stmt).all())

    def _existing_row_map(self, source_key: str, dataset_id: str) -> dict[str, str]:
        """``source_row_ref -> observation_id`` for rows already imported for this dataset (idempotency
        + parent resolution across runs). Scoped to this business."""
        rows = self.imports_for(dataset_id, source_key)
        return {r.source_row_ref: r.observation_id for r in rows}

    # -- the one import path ------------------------------------------------------------------------

    def import_dataset(
        self,
        manifest: DatasetManifest,
        rows: Sequence[DatasetRow],
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> ImportResult:
        """Validate, authorize, and import an approved organization dataset; return an :class:`ImportResult`.

        Order (each stage imports nothing on failure): manifest completeness + streaming bound (raises
        :class:`OrgDatasetImportError`) → row-schema refs (raises) → the DCA-048 rights preflight
        (returns an un-authorized result on denial) → per-row import through the DCA-059 store, with the
        organization-only guard and the ``(source, dataset, row_ref)`` idempotency skip.
        """
        moment = now or datetime.now(UTC)

        # 1) Manifest completeness — a bare / incomplete manifest is rejected before anything is touched.
        dataset_id = (manifest.dataset_id or "").strip()
        source_key = (manifest.source_key or "").strip()
        source_version = (manifest.source_version or "").strip()
        license_ = (manifest.license or "").strip()
        purpose = (manifest.purpose or "").strip()
        if not dataset_id:
            raise OrgDatasetImportError(
                REASON_MISSING_DATASET_ID, "manifest must name a dataset_id"
            )
        if not source_key:
            raise OrgDatasetImportError(
                REASON_MISSING_SOURCE_KEY, "manifest must name the catalog source_key"
            )
        if not source_version:
            raise OrgDatasetImportError(
                REASON_MISSING_SOURCE_VERSION,
                "manifest must carry a source_version (dataset snapshot label)",
            )
        if not license_:
            raise OrgDatasetImportError(
                REASON_MISSING_LICENSE, "manifest must declare the dataset license"
            )
        if not purpose:
            raise OrgDatasetImportError(
                REASON_MISSING_PURPOSE, "manifest must name the purpose the import serves"
            )

        # 2) Streaming bound — refuse an oversized dataset whole, before reading any row.
        if len(rows) > self._max_rows:
            raise OrgDatasetImportError(
                REASON_DATASET_TOO_LARGE,
                f"dataset has {len(rows)} rows; the import bound is {self._max_rows}",
                detail=str(len(rows)),
            )

        # 3) Row schema — every row needs a non-empty, unique checkpointable ref (the idempotency key).
        seen_refs: set[str] = set()
        for row in rows:
            ref = (row.row_ref or "").strip()
            if not ref:
                raise OrgDatasetImportError(
                    REASON_ROW_MISSING_REF, "every dataset row must carry a source_row_ref"
                )
            if ref in seen_refs:
                raise OrgDatasetImportError(
                    REASON_DUPLICATE_ROW_REF,
                    f"dataset row ref '{ref}' appears more than once",
                    detail=ref,
                )
            seen_refs.add(ref)

        # 4) Rights FIRST (DCA-048) — a denial imports nothing and changes no state (a pure read).
        decision = self._preflight.authorize(
            PreflightRequest(
                source_key=source_key,
                operation=IMPORT_OPERATION,
                purpose=purpose,
                field="",
            ),
            now=now,
        )
        if not decision.allowed:
            return ImportResult(
                authorized=False,
                reason=decision.reason,
                dataset_id=dataset_id,
                source_key=source_key,
                source_version=source_version,
            )

        # 5) Import — legal entities first so an establishment's in-dataset parent already exists. The
        #    (source, dataset, row_ref) map gives both the idempotency skip and cross-run parent refs.
        ref_to_observation = self._existing_row_map(source_key, dataset_id)
        imported: list[ImportedRow] = []
        skipped: list[ImportedRow] = []
        refused: list[tuple[str, str]] = []

        for row in sorted(rows, key=_order_key):
            ref = row.row_ref.strip()

            # Idempotency: a row already imported for this dataset is a deterministic no-op.
            if ref in ref_to_observation:
                skipped.append(
                    ImportedRow(ref, ref_to_observation[ref], row.entity_type.strip().lower())
                )
                continue

            # Organization-only guard (DCA-122): a row carrying personal contact fields is refused.
            if row.contact and self.capability.is_organization_only:
                refused.append((ref, REASON_PERSONAL_FIELDS))
                continue

            # Resolve an establishment's parent (by dataset row ref) to the observation it created.
            parent_observation_id: str | None = None
            if (row.entity_type or "").strip().lower() == "establishment":
                parent_ref = (row.parent_row_ref or "").strip()
                parent_observation_id = ref_to_observation.get(parent_ref)
                if parent_observation_id is None:
                    # The parent row was not in this dataset / not yet imported — refuse this row
                    # explicitly rather than silently dropping it or linking cross-tenant.
                    refused.append((ref, REASON_PARENT_ROW_UNRESOLVED))
                    continue

            try:
                observation = self._observations.create(
                    ObservationInput(
                        entity_type=row.entity_type,
                        display_name=row.display_name,
                        parent_observation_id=parent_observation_id,
                        state_code=row.state_code,
                        external_ids=list(row.external_ids),
                    ),
                    actor=actor,
                    now=moment,
                )
            except OrgObservationError as exc:
                # A row with a bad org shape (unknown type, invalid state, duplicate external id, …) is
                # refused per-row; the store validated before mutating, so this row staged nothing.
                refused.append((ref, exc.code))
                continue

            # Record the source attribution + checkpointable row reference alongside the identity.
            self._session.add(
                OrgObservationImport(
                    id=str(uuid.uuid4()),
                    business_id=self._business_id,
                    observation_id=observation.id,
                    source_key=source_key,
                    dataset_id=dataset_id,
                    source_version=source_version,
                    dataset_license=license_,
                    source_row_ref=ref,
                    schema_version=manifest.schema_version,
                    created_at=moment,
                )
            )
            ref_to_observation[ref] = observation.id
            imported.append(ImportedRow(ref, observation.id, row.entity_type.strip().lower()))

        return ImportResult(
            authorized=True,
            reason=None,
            dataset_id=dataset_id,
            source_key=source_key,
            source_version=source_version,
            imported=tuple(imported),
            skipped=tuple(skipped),
            refused_rows=tuple(refused),
        )
