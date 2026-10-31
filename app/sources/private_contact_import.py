"""DCA-126 — import a client's private contact CSV (the fourth concrete source adapter).

Key: ``csv-private-import`` · source sections 6, 8, 9. Stage: Approved source access.

The first three concrete adapters were ORGANIZATION-only: DCA-123 imported an approved org dataset,
DCA-124 extracted org facts from an approved website, DCA-125 enriched an org from a licensed provider —
NONE of them could write a personal contact field (DCA-122's organization-only guard refused it). This is
the FIRST adapter that writes PERSONAL data: it imports a CLIENT'S OWN private contact list — personal
(person + contact-point) data the business already holds — into the tenant-private DCA-060
:class:`~app.db.models.PersonObservation` + :class:`~app.db.models.ContactPoint` store. Because it handles
personal data it carries a capability descriptor that DECLARES it (``handles_personal_contacts=True`` +
:data:`~app.sources.capabilities.AdapterOperation.ENRICH_CONTACT`) — the first adapter for which DCA-122's
personal-contact path is legitimately open, and still validated for coherence at import time.

There is NO network fetch here — the CSV is CALLER-SUPPLIED (a list of already-parsed rows). The importer
adds column mapping + validation and a bounded, deterministic private import path. The load-bearing
guarantees (the DCA-126 acceptance criteria):

- **Imported contact details stay tenant-private.** Every person / contact point is written through the
  DCA-060 store, which is bound to ONE verified business scope; there is no global index on a contact
  value, so one business's imported CSV is NEVER visible to another (tenant isolation, DCA-017/018). The
  same value imported by two businesses stays two independent private rows.

- **Importing a contact is NOT recipient consent.** The import records the contact + the CLIENT-ASSERTED
  lawful ``basis`` (the explicit permission-EVIDENCE status) and provenance — it NEVER fabricates a
  :class:`~app.db.models.ConsentReceipt`, never clears suppression, and keeps the send pipeline a disabled
  future capability. A ``subscribed``-style column the CSV may carry is RECORDED as a client claim only and
  NEVER trusted as consent ("do not trust a subscribed flag").

- **A suppressed / restricted contact is EXCLUDED or flagged.** Each parsed contact point is gated through
  the DCA-079 authoritative suppression lookup AND the DCA-078 local processing restrictions. A value that
  is on the host suppression list, or whose channel/purpose is under an in-force restriction, is NOT stored
  as a contactable :class:`~app.db.models.ContactPoint` — it is excluded and only FLAGGED (opaque
  channel + reason) on the provenance. A duplicate contact point within the business is likewise excluded.

- **Invalid rows have safe row-level errors and no fabricated consent.** A row missing a name, or with no
  parseable/valid contact value at all, is refused with a stable per-row code and imports NOTHING for that
  row (no person, no contact, no consent). Other rows still import.

- **Idempotent re-import.** ``(business_id, dataset_id, source_row_ref)`` is the idempotency key: a row
  already imported for this list is SKIPPED (its existing person reused), so re-running the same import is
  a deterministic no-op.

The importer stages into the caller's session and never commits — the caller owns the unit of work, so the
persons, their contact points, their atomic audit and the provenance rows all commit together. A manifest
problem is raised BEFORE the session is mutated, so a refusal imports nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor
from app.db.models import (
    ContactChannel,
    ContactEvidence,
    ContactImport,
    ContactImportBasis,
    ContactImportStatus,
    ProcessingRestriction,
    RestrictionAuthorityPlane,
    RestrictionRetentionMode,
    SourceAccessMethod,
    SourceKind,
    SourceOperation,
)
from app.normalization.domains import normalize_domain
from app.normalization.errors import NormalizationError
from app.normalization.phones import STATUS_NORMALIZED, normalize_phone
from app.services.contact_point import (
    ContactPointError,
    ContactPointInput,
    ContactPointStore,
    PersonInput,
)
from app.sources.capabilities import (
    AdapterOperation,
    SourceAdapterCapability,
    validate_capability,
)
from app.suppression.contract import ActivationDecision, SuppressionSnapshot, authorize_activation

# The adapter key (distinct from a catalog Source key — a private CSV has no external catalog source).
ADAPTER_KEY = "dca.private_contact_import"

# The PERSONAL-DATA capability this adapter declares (DCA-122). It writes personal contact points, so —
# unlike the three organization-only adapters — ``handles_personal_contacts`` is True and it declares the
# ONLY personal-data operation, ``ENRICH_CONTACT``. Validated at import time so an incoherent descriptor
# fails loudly on load (and so this is the first proof DCA-122's personal-contact path is coherent).
PRIVATE_CONTACT_IMPORT_CAPABILITY: SourceAdapterCapability = validate_capability(
    SourceAdapterCapability(
        key=ADAPTER_KEY,
        source_kind=SourceKind.FIRST_PARTY,
        access_method=SourceAccessMethod.MANUAL,
        operations=frozenset(
            {
                AdapterOperation.LIST_CAPABILITIES,
                AdapterOperation.ENRICH_CONTACT,
            }
        ),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=True,
        trust_tier="first_party_client_supplied",
        notes="DCA-126 client private contact CSV importer (first-party personal data, caller-supplied, "
        "no fetch; importing is NOT recipient consent)",
    )
)

# The version of the import-provenance contract rows are written under (expand→migrate→contract).
IMPORT_SCHEMA_VERSION = 1

# A conservative bound: a single import may carry at most this many rows. A larger list is refused whole
# (import nothing) rather than loaded unbounded.
DEFAULT_MAX_IMPORT_ROWS = 10_000

# The controlled vocabulary of client-asserted bases the manifest may declare.
BASES: tuple[str, ...] = tuple(b.value for b in ContactImportBasis)

# The contact columns the importer maps, in a stable order: (channel, ColumnMapping attribute).
_CONTACT_COLUMNS: tuple[tuple[str, str], ...] = (
    (ContactChannel.EMAIL.value, "email"),
    (ContactChannel.MOBILE.value, "mobile"),
    (ContactChannel.WHATSAPP.value, "whatsapp"),
    (ContactChannel.LANDLINE.value, "landline"),
)

# Map a tenant-private contact channel to the channel token the host suppression registry speaks for
# (DCA-079). A mobile/WhatsApp number is suppressed on the messaging channel; email on email. A channel
# the snapshot does not authoritatively cover is simply not excluded on suppression grounds (storing a
# first-party contact is not contacting it — the send pipeline re-checks suppression authoritatively).
_SUPPRESSION_CHANNEL: dict[str, str] = {
    ContactChannel.EMAIL.value: "email",
    ContactChannel.MOBILE.value: "sms",
    ContactChannel.WHATSAPP.value: "whatsapp",
    ContactChannel.LANDLINE.value: "voice",
}

# Truthy spellings of a CSV ``subscribed`` column — RECORDED as a client claim only, NEVER trusted.
_TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "y", "subscribed", "opted_in", "opt_in"})

# Manifest-level refusals — each raised BEFORE the session is mutated, so a refusal imports nothing.
REASON_MISSING_DATASET_ID = "manifest_missing_dataset_id"
REASON_MISSING_BASIS = "manifest_missing_basis"
REASON_UNKNOWN_BASIS = "manifest_unknown_basis"
REASON_DATASET_TOO_LARGE = "dataset_exceeds_row_bound"
REASON_DUPLICATE_ROW_REF = "duplicate_source_row_ref"

# Per-row refusal reasons (the row imports NOTHING — no person, no contact, no consent).
REASON_ROW_MISSING_NAME = "row_missing_name"
REASON_ROW_NO_CONTACT_VALUE = "row_no_contact_value"
REASON_ROW_NO_VALID_CONTACT = "row_no_valid_contact_value"

# Per-contact-point EXCLUSION flags (the person is kept; the value is NOT stored as contactable).
REASON_SUPPRESSED = "contact_point_suppressed"
REASON_RESTRICTED = "processing_restricted"
REASON_DUPLICATE_CONTACT = "duplicate_contact_point"
REASON_INVALID_VALUE = "invalid_contact_value"


class PrivateContactImportError(Exception):
    """A manifest/schema problem refused the import with a stable code. Raised before the session is
    mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending value
    when useful (never a raw contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ColumnMapping:
    """Maps CSV column names to the fields the importer reads. Defaults are the conventional names; a
    caller overrides any that differ. Unmapped columns are ignored (never imported)."""

    name: str = "name"
    role_title: str = "role_title"
    email: str = "email"
    mobile: str = "mobile"
    whatsapp: str = "whatsapp"
    landline: str = "landline"
    subscribed: str = "subscribed"
    row_ref: str = "row_ref"


@dataclass(frozen=True)
class PrivateImportManifest:
    """The manifest describing ONE private contact-list import.

    ``dataset_id`` is the opaque id of the import batch/list (the idempotency namespace). ``basis`` is the
    CLIENT-ASSERTED lawful basis (a :class:`~app.db.models.ContactImportBasis` value) — the explicit
    permission-EVIDENCE status; it is recorded for review and is NEVER recipient consent. ``purpose`` is
    the purpose the import serves (restrictions are purpose-bound; "" = unspecified). ``source_label`` /
    ``source_version`` are the client's own provenance labels recorded on every row. ``mapping`` names the
    CSV columns. ``dataset_id`` and a known ``basis`` are REQUIRED; an incomplete manifest imports nothing.
    """

    dataset_id: str
    basis: str
    purpose: str = ""
    source_label: str = ""
    source_version: str = ""
    mapping: ColumnMapping = field(default_factory=ColumnMapping)
    schema_version: int = IMPORT_SCHEMA_VERSION


@dataclass(frozen=True)
class ImportedContact:
    """One stored contact point's outcome: the channel and the contact-point row it maps to."""

    channel: str
    contact_point_id: str


@dataclass(frozen=True)
class ExcludedContact:
    """One EXCLUDED contact point: the channel and the stable reason it was not stored (opaque — never a
    raw contact value)."""

    channel: str
    reason: str


@dataclass(frozen=True)
class ImportedRow:
    """One CSV row's outcome: the checkpointable ref, the person it maps to, the per-row ``status`` and
    the stored / excluded contact points."""

    row_ref: str
    person_observation_id: str
    status: str
    imported: tuple[ImportedContact, ...] = ()
    excluded: tuple[ExcludedContact, ...] = ()


@dataclass(frozen=True)
class RowError:
    """One MALFORMED CSV row that imported nothing (no person / contact / consent), with a stable code."""

    row_ref: str
    reason: str


@dataclass(frozen=True)
class ImportResult:
    """The deterministic outcome of one private-contact import run (nothing fetched).

    ``imported`` are the rows newly recorded this run, ``skipped`` the rows already imported for this list
    (the idempotent no-op), and ``row_errors`` the malformed rows refused per-row (each imported nothing).
    """

    dataset_id: str
    imported: tuple[ImportedRow, ...] = ()
    skipped: tuple[ImportedRow, ...] = ()
    row_errors: tuple[RowError, ...] = ()

    @property
    def imported_count(self) -> int:
        return len(self.imported)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def error_count(self) -> int:
        return len(self.row_errors)

    @property
    def contacts_imported(self) -> int:
        return sum(len(r.imported) for r in self.imported)

    @property
    def contacts_excluded(self) -> int:
        return sum(len(r.excluded) for r in self.imported)


def _as_utc(moment: datetime) -> datetime:
    """Normalize to a tz-aware UTC datetime (SQLite returns naive datetimes; assume stored-UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _normalize_email_value(raw: str) -> str:
    """Canonical ``local@host`` (local lower-cased, host via the DCA-064 domain normalizer); raises
    :class:`~app.normalization.errors.NormalizationError` on a value that is not one ``local@host``."""
    trimmed = (raw or "").strip()
    if trimmed.count("@") != 1:
        raise NormalizationError(
            "invalid_email", "an email must be a single local@host address", detail=trimmed
        )
    local_raw, host_raw = trimmed.split("@", 1)
    local = local_raw.strip().lower()
    if not local:
        raise NormalizationError(
            "invalid_email", "an email must have a non-empty local part", detail=trimmed
        )
    domain = normalize_domain(host_raw)  # raises NormalizationError on an invalid host
    return f"{local}@{domain.ascii}"


def _normalize_contact_value(channel: str, raw: str) -> str:
    """Normalize a contact value for storage + suppression matching, or raise
    :class:`~app.normalization.errors.NormalizationError` for a malformed value.

    Email → canonical ``local@host``; phone channels → E.164 (India plan). An ambiguous/unresolved phone
    (no supported country evidence) is treated as invalid for an import (we never guess a number)."""
    if channel == ContactChannel.EMAIL.value:
        return _normalize_email_value(raw)
    # Phone channels: require a resolvable Indian number. An UNRESOLVED result is refused here (a bad
    # value must not become a contactable record), mutating nothing.
    result = normalize_phone(raw, country="IN")
    if result.status != STATUS_NORMALIZED or not result.e164:
        raise NormalizationError(
            "invalid_phone",
            f"a {channel} value could not be normalized",
            detail=(raw or "").strip(),
        )
    return result.e164


class PrivateContactImporter:
    """Import a client's OWN private contact list into tenant-private persons + contact points, for ONE
    business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from the
    membership-verified tenant context). Composes the DCA-060 contact-point store (the single sanctioned
    writer of persons/contact points, with atomic audit) with the DCA-079 suppression lookup and the
    DCA-078 local processing restrictions. It records the contact + the client-asserted basis/provenance —
    it NEVER fabricates consent and never clears suppression.
    """

    capability: SourceAdapterCapability = PRIVATE_CONTACT_IMPORT_CAPABILITY

    def __init__(
        self, session: Session, business_id: str, *, max_rows: int = DEFAULT_MAX_IMPORT_ROWS
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._max_rows = max_rows
        self._contacts = ContactPointStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def imports_for(self, dataset_id: str) -> list[ContactImport]:
        """Every import-provenance row recorded for one list, in this business (oldest first)."""
        stmt = (
            select(ContactImport)
            .where(ContactImport.business_id == self._business_id)
            .where(ContactImport.dataset_id == (dataset_id or "").strip())
            .order_by(ContactImport.created_at, ContactImport.id)
        )
        return list(self._session.scalars(stmt).all())

    def _existing_row_map(self, dataset_id: str) -> dict[str, ContactImport]:
        """``source_row_ref -> provenance row`` for rows already imported for this list (idempotency)."""
        return {r.source_row_ref: r for r in self.imports_for(dataset_id)}

    # -- the one import path ------------------------------------------------------------------------

    def import_contacts(
        self,
        manifest: PrivateImportManifest,
        rows: Sequence[Mapping[str, object]],
        actor: AuditActor,
        *,
        suppression: SuppressionSnapshot | None = None,
        now: datetime | None = None,
    ) -> ImportResult:
        """Validate, map and import a client's private contact list; return an :class:`ImportResult`.

        Order (each stage imports nothing on failure): manifest completeness + streaming bound + unique
        row refs (raises :class:`PrivateContactImportError`) → per-row mapping/validation (a malformed row
        becomes a :class:`RowError` and imports nothing) → idempotency skip → person + gated contact
        points through the DCA-060 store, with each contact point screened by the DCA-079 suppression
        snapshot and the DCA-078 restrictions (a suppressed/restricted/duplicate value is excluded and
        flagged) → the provenance row with the client-asserted basis + the (untrusted) subscribed claim.
        """
        moment = now or datetime.now(UTC)
        mapping = manifest.mapping

        # 1) Manifest completeness — a bare / incomplete manifest is rejected before anything is touched.
        dataset_id = (manifest.dataset_id or "").strip()
        if not dataset_id:
            raise PrivateContactImportError(
                REASON_MISSING_DATASET_ID, "manifest must name a dataset_id"
            )
        basis_raw = (manifest.basis or "").strip().lower()
        if not basis_raw:
            raise PrivateContactImportError(
                REASON_MISSING_BASIS, "manifest must declare the client-asserted lawful basis"
            )
        if basis_raw not in BASES:
            raise PrivateContactImportError(
                REASON_UNKNOWN_BASIS,
                f"unknown basis '{manifest.basis}'; expected one of: {', '.join(BASES)}",
                detail=manifest.basis,
            )
        basis = ContactImportBasis(basis_raw).value
        purpose = (manifest.purpose or "").strip().lower()
        source_label = (manifest.source_label or "").strip()
        source_version = (manifest.source_version or "").strip()

        # 2) Streaming bound — refuse an oversized list whole, before reading any row.
        if len(rows) > self._max_rows:
            raise PrivateContactImportError(
                REASON_DATASET_TOO_LARGE,
                f"list has {len(rows)} rows; the import bound is {self._max_rows}",
                detail=str(len(rows)),
            )

        # 3) Row refs — derive a stable, unique checkpointable ref per row (the idempotency key).
        refs: list[str] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            ref = str(row.get(mapping.row_ref, "") or "").strip() or str(index + 1)
            if ref in seen:
                raise PrivateContactImportError(
                    REASON_DUPLICATE_ROW_REF,
                    f"row ref '{ref}' appears more than once",
                    detail=ref,
                )
            seen.add(ref)
            refs.append(ref)

        # 4) Import — the suppression snapshot + the in-force restrictions are read once for the run.
        existing = self._existing_row_map(dataset_id)
        restricted = self._restricted_channels(purpose, moment)

        imported: list[ImportedRow] = []
        skipped: list[ImportedRow] = []
        row_errors: list[RowError] = []

        for ref, row in zip(refs, rows, strict=True):
            # Idempotency: a row already imported for this list is a deterministic no-op.
            prior = existing.get(ref)
            if prior is not None:
                skipped.append(ImportedRow(ref, prior.person_observation_id, prior.status))
                continue

            outcome = self._import_row(
                ref=ref,
                row=row,
                mapping=mapping,
                dataset_id=dataset_id,
                manifest_basis=basis,
                purpose=purpose,
                source_label=source_label,
                source_version=source_version,
                schema_version=manifest.schema_version,
                actor=actor,
                suppression=suppression,
                restricted=restricted,
                moment=moment,
            )
            if isinstance(outcome, RowError):
                row_errors.append(outcome)
            else:
                imported.append(outcome)

        return ImportResult(
            dataset_id=dataset_id,
            imported=tuple(imported),
            skipped=tuple(skipped),
            row_errors=tuple(row_errors),
        )

    # -- internals ----------------------------------------------------------------------------------

    def _import_row(
        self,
        *,
        ref: str,
        row: Mapping[str, object],
        mapping: ColumnMapping,
        dataset_id: str,
        manifest_basis: str,
        purpose: str,
        source_label: str,
        source_version: str,
        schema_version: int,
        actor: AuditActor,
        suppression: SuppressionSnapshot | None,
        restricted: frozenset[str],
        moment: datetime,
    ) -> ImportedRow | RowError:
        """Validate and import ONE mapped row. Returns a :class:`RowError` (imports nothing) for a
        malformed row, or an :class:`ImportedRow` for a row that produced a person (with its stored /
        excluded contact points)."""
        display_name = str(row.get(mapping.name, "") or "").strip()
        if not display_name:
            return RowError(ref, REASON_ROW_MISSING_NAME)

        # Parse the mapped contact columns. A non-empty value is normalized; a malformed value is noted.
        valid: list[tuple[str, str]] = []
        invalid: list[ExcludedContact] = []
        present = 0
        for channel, column in _CONTACT_COLUMNS:
            raw = str(row.get(column, "") or "").strip()
            if not raw:
                continue
            present += 1
            try:
                valid.append((channel, _normalize_contact_value(channel, raw)))
            except NormalizationError:
                # A malformed value is never stored; it is noted so the row's outcome stays honest.
                invalid.append(ExcludedContact(channel, REASON_INVALID_VALUE))

        if present == 0:
            # A row with a name but NO contact column at all is malformed for a contact import.
            return RowError(ref, REASON_ROW_NO_CONTACT_VALUE)
        if not valid:
            # Every contact value on the row was malformed — the whole row is refused (no person).
            return RowError(ref, REASON_ROW_NO_VALID_CONTACT)

        # The row is importable: create the tenant-private person identity (audited by the DCA-060 store).
        person = self._contacts.create_person(
            PersonInput(
                display_name=display_name,
                role_title=str(row.get(mapping.role_title, "") or "").strip(),
            ),
            actor=actor,
            now=moment,
        )

        stored: list[ImportedContact] = []
        excluded: list[ExcludedContact] = list(invalid)
        for channel, value in valid:
            reason = self._gate(channel, value, suppression, restricted, moment)
            if reason is not None:
                # Suppressed / restricted: NOT stored as a contactable record — excluded and flagged.
                excluded.append(ExcludedContact(channel, reason))
                continue
            try:
                cp = self._contacts.add_contact_point(
                    ContactPointInput(
                        person_observation_id=person.id,
                        channel=channel,
                        value=value,
                        evidence=ContactEvidence.IMPORTED.value,
                    ),
                    actor=actor,
                    now=moment,
                )
            except ContactPointError as exc:
                # A duplicate (or other per-contact refusal) within the business — excluded, never crashes
                # the import. The code is opaque; the raw value is never surfaced.
                code = (
                    REASON_DUPLICATE_CONTACT if exc.code == "duplicate_contact_point" else exc.code
                )
                excluded.append(ExcludedContact(channel, code))
                continue
            stored.append(ImportedContact(channel, cp.id))

        if stored and excluded:
            status = ContactImportStatus.PARTIALLY_EXCLUDED.value
        elif stored:
            status = ContactImportStatus.IMPORTED.value
        else:
            status = ContactImportStatus.EXCLUDED.value

        subscribed = str(row.get(mapping.subscribed, "") or "").strip().lower() in _TRUTHY

        # Stage the provenance: the person, the client-asserted basis (permission-evidence), the UNTRUSTED
        # subscribed claim, and opaque exclusion flags (channel + reason only — never a raw contact value).
        self._session.add(
            ContactImport(
                id=str(uuid.uuid4()),
                business_id=self._business_id,
                person_observation_id=person.id,
                dataset_id=dataset_id,
                source_label=source_label,
                source_version=source_version,
                source_row_ref=ref,
                basis=manifest_basis,
                purpose=purpose,
                asserted_subscribed=subscribed,
                status=status,
                contacts_imported=len(stored),
                contacts_excluded=len(excluded),
                exclusion_flags=[[e.channel, e.reason] for e in excluded],
                schema_version=schema_version,
                created_at=moment,
            )
        )
        return ImportedRow(ref, person.id, status, tuple(stored), tuple(excluded))

    def _gate(
        self,
        channel: str,
        value: str,
        suppression: SuppressionSnapshot | None,
        restricted: frozenset[str],
        moment: datetime,
    ) -> str | None:
        """Decide whether a contact point may be stored. Returns a stable EXCLUSION reason (suppressed /
        restricted) or ``None`` when it may be stored.

        Suppression (DCA-079): a value the host authoritatively reports as SUPPRESSED on this channel is
        excluded. Other suppression outcomes (unavailable / stale / channel-not-covered) do NOT block the
        import — storing a first-party contact is not contacting it, and the send pipeline re-checks
        suppression authoritatively before anything could ever be sent.

        Restrictions (DCA-078): a channel under an in-force local processing restriction for this purpose
        (or a tenant-wide / all-channel restriction) is excluded.
        """
        # Restriction takes precedence as the tenant's own explicit instruction.
        if channel in restricted or "" in restricted:
            return REASON_RESTRICTED
        if suppression is not None:
            sup_channel = _SUPPRESSION_CHANNEL.get(channel, channel)
            decision = authorize_activation(suppression, value, sup_channel, now=moment)
            if not decision.allowed and decision.reason == ActivationDecision.SUPPRESSED:
                return REASON_SUPPRESSED
        return None

    def _restricted_channels(self, purpose: str, moment: datetime) -> frozenset[str]:
        """The channels restricted for this import by in-force LOCAL processing restrictions (DCA-078).

        A restriction matches the import when it is BUSINESS-plane, brand-agnostic (``scope_brand`` empty),
        and its ``scope_purpose`` is empty (all purposes) or equals this import's purpose. A matching
        restriction's ``scope_channel`` is restricted; an empty ``scope_channel`` ("" = all channels) is
        returned as ``""`` in the set, which :meth:`_gate` treats as restricting every channel. Lifting a
        restriction is a disabled future capability, so a recorded restriction stands unless it was
        ``until_date`` with a ``retain_until`` that has now lapsed.
        """
        stmt = (
            select(ProcessingRestriction)
            .where(ProcessingRestriction.business_id == self._business_id)
            .where(
                ProcessingRestriction.authority_plane == RestrictionAuthorityPlane.BUSINESS.value
            )
        )
        channels: set[str] = set()
        for r in self._session.scalars(stmt).all():
            if (r.scope_brand or "").strip():
                continue  # a brand-specific restriction does not apply to a brand-agnostic import
            scope_purpose = (r.scope_purpose or "").strip().lower()
            if scope_purpose and scope_purpose != purpose:
                continue
            if (
                r.retention_mode == RestrictionRetentionMode.UNTIL_DATE.value
                and r.retain_until is not None
                and _as_utc(r.retain_until) <= moment
            ):
                continue  # a dated restriction that has lapsed no longer stands
            channels.add((r.scope_channel or "").strip().lower())
        return frozenset(channels)


__all__ = [
    "ADAPTER_KEY",
    "PRIVATE_CONTACT_IMPORT_CAPABILITY",
    "IMPORT_SCHEMA_VERSION",
    "DEFAULT_MAX_IMPORT_ROWS",
    "ColumnMapping",
    "PrivateImportManifest",
    "ImportedContact",
    "ExcludedContact",
    "ImportedRow",
    "RowError",
    "ImportResult",
    "PrivateContactImporter",
    "PrivateContactImportError",
    "REASON_MISSING_DATASET_ID",
    "REASON_MISSING_BASIS",
    "REASON_UNKNOWN_BASIS",
    "REASON_DATASET_TOO_LARGE",
    "REASON_DUPLICATE_ROW_REF",
    "REASON_ROW_MISSING_NAME",
    "REASON_ROW_NO_CONTACT_VALUE",
    "REASON_ROW_NO_VALID_CONTACT",
    "REASON_SUPPRESSED",
    "REASON_RESTRICTED",
    "REASON_DUPLICATE_CONTACT",
    "REASON_INVALID_VALUE",
]
