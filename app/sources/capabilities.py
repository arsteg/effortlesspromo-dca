"""DCA-122 — define the Python source-adapter capability contract.

Key: ``source-protocol`` · source sections 6, 8, 9. Stage: Approved source access.

This story DEFINES (it does not wire or enable) the typed contract every concrete source adapter
(DCA-123..127: org-dataset import, website extract, licensed provider, private CSV, caching) will
declare and implement. It ships three pure things — a capability *descriptor* an adapter publishes, a
typed *Protocol/ABC* it implements, and a *registry* that validates a declaration is coherent — and no
live adapter, no fetch, and no enabled source. It composes with, and never bypasses, the already-built
source-rights chain (DCA-044..048) and the fetch guards (DCA-118..121); it adds no acquisition.

The load-bearing guarantee (the DCA-122 acceptance criteria):

- **An unsupported capability fails EXPLICITLY — never a silent empty result.** An adapter does only
  what its descriptor DECLARES. Every operation call is gated through the descriptor
  (:meth:`SourceAdapterCapability.assert_supports` / :class:`BaseSourceAdapter`), so asking an adapter
  for an operation it does not declare raises :class:`UnsupportedCapabilityError` instead of quietly
  returning nothing — a caller can tell "no results" apart from "not supported".

- **An organization-only adapter can NEVER silently fetch personal contacts.** Contact enrichment is
  the only personal-data operation. A descriptor may declare :data:`AdapterOperation.ENRICH_CONTACT`
  ONLY if it ALSO declares it handles personal contact data AND declares the source right
  (:data:`SourceOperation.COLLECT`) personal retrieval requires. :func:`validate_capability` rejects an
  organization-only descriptor that claims contact enrichment (``personal_contact_not_declared``) before
  it can ever be registered, and the ABC still refuses the call at runtime — so an org-only adapter has
  no path, declared or executed, to personal contacts.

- **An adapter cannot claim an operation it is not entitled for.** Each :class:`AdapterOperation` has a
  minimal set of :class:`SourceOperation` rights it depends on (:data:`REQUIRED_RIGHTS`). A descriptor
  that supports an operation without DECLARING the right that operation needs is incoherent and rejected
  (``required_rights_not_declared``). :meth:`SourceAdapterCapability.permitted_operations` then lets a
  later (still disabled) consumer intersect the declared operations with the rights the DCA-044..048
  chain actually GRANTS, so an operation whose right the rights chain withholds is simply not permitted.

Validation runs FIRST and raises :class:`CapabilityValidationError` with a stable code, mirroring the
DCA-044 source-catalog store; a rejected descriptor leaves the registry untouched. Nothing here fetches:
:data:`CONCRETE_ADAPTERS_ENABLED` is ``False`` and every concrete adapter stays a disabled future story.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.compliance.rulepacks import RawCandidate
from app.db.models import SourceAccessMethod, SourceKind, SourceOperation

# No concrete adapter is wired or enabled by this story. The descriptor/Protocol/registry are pure
# definitions; the live adapters (DCA-123..127) and any fetch are disabled future capabilities.
CONCRETE_ADAPTERS_ENABLED = False


class AdapterOperation(str, Enum):
    """The distinct operations a source adapter may declare it supports (DCA-122).

    These are the adapter-surface operations the concrete adapters (DCA-123..127) will implement — not
    to be confused with :class:`~app.db.models.SourceOperation`, which are the per-field *rights* a
    source's terms grant. Each operation below depends on a minimal set of those rights
    (:data:`REQUIRED_RIGHTS`).

    - ``LIST_CAPABILITIES`` — publish this descriptor. Rights-free and MANDATORY: every adapter can be
      asked what it supports before anything else is attempted.
    - ``ESTIMATE`` — approximate how many organizations a query would match, WITHOUT retrieving records
      (a count/size hint only), so it needs no retrieval right.
    - ``DISCOVER_ORGANIZATIONS`` — find organizations matching an ICP query.
    - ``ENRICH_ORGANIZATION`` — fetch additional organization-level fields for a known org.
    - ``ENRICH_CONTACT`` — fetch contact-level fields. The ONLY personal-data operation; declaring it
      requires :attr:`SourceAdapterCapability.handles_personal_contacts`.
    - ``DETECT_CHANGES`` — report organizations changed since a cursor (for refresh).
    - ``DETECT_DELETIONS`` — report organizations removed at the source (for retention/suppression).
    """

    LIST_CAPABILITIES = "list_capabilities"
    ESTIMATE = "estimate"
    DISCOVER_ORGANIZATIONS = "discover_organizations"
    ENRICH_ORGANIZATION = "enrich_organization"
    ENRICH_CONTACT = "enrich_contact"
    DETECT_CHANGES = "detect_changes"
    DETECT_DELETIONS = "detect_deletions"


# The ONE personal-data operation. Kept as a set so the coherence rule reads declaratively and future
# personal operations (if any) are added in exactly one place.
PERSONAL_OPERATIONS: frozenset[AdapterOperation] = frozenset({AdapterOperation.ENRICH_CONTACT})

# The minimal source RIGHTS (DCA-045 per-field operations) each adapter operation depends on. An
# operation that retrieves records needs COLLECT; metadata-only operations (listing, estimation) need
# nothing. This is the floor a descriptor must DECLARE it needs — never an entitlement grant itself.
REQUIRED_RIGHTS: dict[AdapterOperation, frozenset[SourceOperation]] = {
    AdapterOperation.LIST_CAPABILITIES: frozenset(),
    AdapterOperation.ESTIMATE: frozenset(),
    AdapterOperation.DISCOVER_ORGANIZATIONS: frozenset({SourceOperation.COLLECT}),
    AdapterOperation.ENRICH_ORGANIZATION: frozenset({SourceOperation.COLLECT}),
    AdapterOperation.ENRICH_CONTACT: frozenset({SourceOperation.COLLECT}),
    AdapterOperation.DETECT_CHANGES: frozenset({SourceOperation.COLLECT}),
    AdapterOperation.DETECT_DELETIONS: frozenset({SourceOperation.COLLECT}),
}


# Stable validation codes — each raised by :func:`validate_capability` BEFORE a descriptor is stored,
# so a rejected declaration leaves the registry untouched (mirrors DCA-044's source-catalog codes).
REASON_MISSING_KEY = "missing_adapter_key"
REASON_NO_OPERATIONS = "no_operations_declared"
REASON_LISTING_REQUIRED = "capability_listing_required"
REASON_RIGHTS_NOT_DECLARED = "required_rights_not_declared"
REASON_PERSONAL_CONTACT_NOT_DECLARED = "personal_contact_not_declared"
REASON_PERSONAL_WITHOUT_CONTACT_OP = "personal_flag_without_contact_operation"
REASON_DUPLICATE_KEY = "duplicate_adapter_key"

# The denial a caller sees when it asks an adapter for an operation the descriptor does not declare.
REASON_OPERATION_NOT_SUPPORTED = "operation_not_supported"


class CapabilityValidationError(Exception):
    """A capability descriptor was refused with a stable code. Raised before the registry is mutated,
    so a refusal stores nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class UnsupportedCapabilityError(Exception):
    """Raised when an adapter is asked for an operation its descriptor does not declare.

    This is the EXPLICIT failure that keeps an unsupported capability from degrading into a silent empty
    result — in particular it is how an organization-only adapter refuses contact enrichment at runtime.
    """

    def __init__(self, adapter_key: str, operation: AdapterOperation) -> None:
        super().__init__(
            f"adapter '{adapter_key}' does not support operation '{operation.value}' "
            f"({REASON_OPERATION_NOT_SUPPORTED})"
        )
        self.code = REASON_OPERATION_NOT_SUPPORTED
        self.adapter_key = adapter_key
        self.operation = operation


class UnknownAdapterError(KeyError):
    """Raised by :meth:`CapabilityRegistry.require` for an adapter key that is not registered."""


@dataclass(frozen=True)
class SourceAdapterCapability:
    """What one source adapter DECLARES about itself — a pure, immutable descriptor (no I/O, no fetch).

    Fields:

    - ``key`` — the adapter's stable identifier (distinct from a catalog :class:`~app.db.models.Source`
      key; an adapter may serve a source but is not itself the source).
    - ``source_kind`` / ``access_method`` — descriptive provenance/access metadata (reusing the
      catalog's enums). Declaring them enables nothing on its own.
    - ``operations`` — the :class:`AdapterOperation` set the adapter supports. Must be non-empty and
      must include :data:`AdapterOperation.LIST_CAPABILITIES`.
    - ``required_rights`` — the :class:`~app.db.models.SourceOperation` rights the adapter DECLARES it
      needs. Must COVER the minimal rights of every supported operation (:data:`REQUIRED_RIGHTS`); it is
      a declaration the later rights chain checks, never a grant.
    - ``handles_personal_contacts`` — whether the adapter touches personal contact data. Must be ``True``
      iff ``operations`` includes :data:`AdapterOperation.ENRICH_CONTACT`. An organization-only adapter
      leaves this ``False`` and therefore cannot declare contact enrichment at all.
    - ``trust_tier`` / ``notes`` — advisory metadata.

    Construct freely, but a descriptor is only coherent once :func:`validate_capability` accepts it;
    :class:`CapabilityRegistry.register` validates before storing.
    """

    key: str
    source_kind: SourceKind
    access_method: SourceAccessMethod
    operations: frozenset[AdapterOperation]
    required_rights: frozenset[SourceOperation] = frozenset()
    handles_personal_contacts: bool = False
    trust_tier: str = ""
    notes: str = ""

    @property
    def is_organization_only(self) -> bool:
        """True when the adapter touches no personal contact data — the org-only case the acceptance
        criterion protects. Such an adapter can neither declare nor perform contact enrichment."""
        return not self.handles_personal_contacts

    def supports(self, operation: AdapterOperation) -> bool:
        """Does the descriptor declare ``operation``? Pure, side-effect-free."""
        return operation in self.operations

    def assert_supports(self, operation: AdapterOperation) -> None:
        """Raise :class:`UnsupportedCapabilityError` unless ``operation`` is declared.

        The single explicit-failure gate every operation call passes through, so an unsupported
        capability can never silently return an empty result.
        """
        if operation not in self.operations:
            raise UnsupportedCapabilityError(self.key, operation)

    @staticmethod
    def minimal_rights_for(operation: AdapterOperation) -> frozenset[SourceOperation]:
        """The minimal source rights ``operation`` depends on (:data:`REQUIRED_RIGHTS`)."""
        return REQUIRED_RIGHTS.get(operation, frozenset())

    def permitted_operations(
        self, granted_rights: Iterable[SourceOperation]
    ) -> frozenset[AdapterOperation]:
        """The supported operations whose minimal rights are ALL within ``granted_rights``.

        Pure: given the rights the DCA-044..048 chain actually grants a business for a source, this is
        the subset of declared operations that chain would permit. An operation whose required right is
        withheld (e.g. no ``COLLECT``) simply does not appear — the adapter cannot do what it is not
        entitled for, independently of what it declared.
        """
        granted = frozenset(granted_rights)
        return frozenset(op for op in self.operations if self.minimal_rights_for(op) <= granted)


def validate_capability(cap: SourceAdapterCapability) -> SourceAdapterCapability:
    """Validate a descriptor is COHERENT; return it unchanged, or raise :class:`CapabilityValidationError`.

    Checks, in order (each raised before any caller stores the descriptor):

    1. ``key`` is present.
    2. ``operations`` is non-empty and includes :data:`AdapterOperation.LIST_CAPABILITIES`.
    3. ``required_rights`` COVERS the minimal rights of every supported operation (no over-claiming an
       operation whose right the adapter has not declared it needs).
    4. The personal-contact coherence rule, both directions: contact enrichment requires the
       personal-data flag (an org-only adapter cannot claim it), and the flag requires a personal
       operation (no dangling personal footprint).

    Pure and reproducible: it reads and changes nothing, so the same descriptor always yields the same
    verdict.
    """
    key = (cap.key or "").strip()
    if not key:
        raise CapabilityValidationError(REASON_MISSING_KEY, "a source adapter key is required")

    if not cap.operations:
        raise CapabilityValidationError(
            REASON_NO_OPERATIONS, f"adapter '{key}' declares no operations", detail=key
        )

    if AdapterOperation.LIST_CAPABILITIES not in cap.operations:
        raise CapabilityValidationError(
            REASON_LISTING_REQUIRED,
            f"adapter '{key}' must support capability listing "
            f"('{AdapterOperation.LIST_CAPABILITIES.value}')",
            detail=key,
        )

    # Over-claim check: every supported operation's minimal rights must be DECLARED in required_rights.
    needed: set[SourceOperation] = set()
    for op in cap.operations:
        needed |= REQUIRED_RIGHTS.get(op, frozenset())
    missing = needed - set(cap.required_rights)
    if missing:
        missing_names = ", ".join(sorted(r.value for r in missing))
        raise CapabilityValidationError(
            REASON_RIGHTS_NOT_DECLARED,
            f"adapter '{key}' supports operations needing rights it does not declare: {missing_names}",
            detail=missing_names,
        )

    claims_contact = bool(cap.operations & PERSONAL_OPERATIONS)
    if claims_contact and not cap.handles_personal_contacts:
        raise CapabilityValidationError(
            REASON_PERSONAL_CONTACT_NOT_DECLARED,
            f"organization-only adapter '{key}' cannot declare contact enrichment "
            f"('{AdapterOperation.ENRICH_CONTACT.value}') without handling personal contact data",
            detail=key,
        )
    if cap.handles_personal_contacts and not claims_contact:
        raise CapabilityValidationError(
            REASON_PERSONAL_WITHOUT_CONTACT_OP,
            f"adapter '{key}' declares it handles personal contacts but supports no personal operation",
            detail=key,
        )

    return cap


class CapabilityRegistry:
    """An in-memory catalog of source-adapter capability descriptors, keyed by adapter key.

    Every :meth:`register` validates the descriptor first (an invalid one raises and is NOT stored) and
    rejects a duplicate key. The registry holds DECLARATIONS only — registering a capability wires no
    live adapter and performs no fetch; the concrete adapters remain a disabled future story. In the
    running service nothing registers an adapter, so the registry is empty; tests and the manual-check
    runner construct descriptors to exercise it.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, SourceAdapterCapability] = {}

    def register(self, cap: SourceAdapterCapability) -> SourceAdapterCapability:
        """Validate and store ``cap``; return the stored descriptor. Raises on an invalid or duplicate
        descriptor, leaving the registry unchanged."""
        validate_capability(cap)
        key = cap.key.strip()
        if key in self._by_key:
            raise CapabilityValidationError(
                REASON_DUPLICATE_KEY,
                f"an adapter with key '{key}' is already registered",
                detail=key,
            )
        self._by_key[key] = cap
        return cap

    def get(self, key: str) -> SourceAdapterCapability | None:
        """One descriptor by adapter key, or ``None``."""
        return self._by_key.get((key or "").strip())

    def require(self, key: str) -> SourceAdapterCapability:
        """One descriptor by adapter key, or raise :class:`UnknownAdapterError`."""
        cap = self.get(key)
        if cap is None:
            raise UnknownAdapterError(f"no source adapter registered with key '{key}'")
        return cap

    def all(self) -> list[SourceAdapterCapability]:
        """Every registered descriptor, sorted by key (stable listing for operators/audit)."""
        return [self._by_key[k] for k in sorted(self._by_key)]

    def keys(self) -> list[str]:
        """Every registered adapter key, sorted."""
        return sorted(self._by_key)

    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.strip() in self._by_key


# --- the typed adapter surface (shapes + Protocol + ABC) ------------------------------------------
#
# These are the SHAPES concrete adapters (DCA-123..127) will exchange. They are intentionally minimal
# definitions — no adapter implements them here, and nothing fetches.


@dataclass(frozen=True)
class DiscoveryQuery:
    """A would-be discovery/estimation query (shape only; no adapter consumes it in this story)."""

    industries: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    limit: int = 0


@dataclass(frozen=True)
class CountEstimate:
    """An approximate match-count hint — no records retrieved, so :data:`AdapterOperation.ESTIMATE`
    needs no retrieval right. ``is_exact`` marks a source that can answer precisely."""

    approximate_total: int
    is_exact: bool = False


@dataclass(frozen=True)
class ChangeSet:
    """Changes since a cursor: organizations changed (as :class:`RawCandidate`) and refs removed at the
    source, plus the ``cursor`` to resume from. Shape only; nothing produces one in this story."""

    changed: tuple[RawCandidate, ...] = ()
    deleted_refs: tuple[str, ...] = ()
    cursor: str = ""


@runtime_checkable
class SourceAdapter(Protocol):
    """The typed contract a concrete source adapter implements (DCA-123..127).

    An adapter publishes its :attr:`capability` (the listing) and exposes the operation methods; a
    caller MUST gate every operation through ``capability.assert_supports(...)`` (or use
    :class:`BaseSourceAdapter`, which does so), so an unsupported operation fails explicitly rather than
    returning a silent empty result. Optional contact enrichment is present on the surface but an
    organization-only adapter refuses it. The only implementations today are test fakes; every live
    adapter is a disabled future story and performs no fetch.
    """

    @property
    def capability(self) -> SourceAdapterCapability: ...

    def estimate(self, query: DiscoveryQuery) -> CountEstimate: ...

    def discover_organizations(self, query: DiscoveryQuery) -> list[RawCandidate]: ...

    def enrich_organization(self, org_ref: str) -> RawCandidate | None: ...

    def enrich_contact(self, org_ref: str) -> list[RawCandidate]: ...

    def detect_changes(self, cursor: str = "") -> ChangeSet: ...


class BaseSourceAdapter(abc.ABC):
    """ABC the concrete adapters (DCA-123..127) build on; encodes the load-bearing guarantee in ONE place.

    Each public operation method first calls :meth:`SourceAdapterCapability.assert_supports`, so an
    adapter can NEVER silently serve an operation it does not declare — an org-only adapter's
    :meth:`enrich_contact` raises :class:`UnsupportedCapabilityError` before any work. A subclass
    implements only the private ``_do_*`` hooks for the operations it declares; the defaults raise
    ``NotImplementedError`` because no live adapter exists yet (concrete fetching is a disabled future
    capability).
    """

    @property
    @abc.abstractmethod
    def capability(self) -> SourceAdapterCapability:
        """The adapter's declared descriptor (the capability-listing surface)."""

    # -- gated public surface (do not override; override the _do_* hooks) ---------------------------

    def estimate(self, query: DiscoveryQuery) -> CountEstimate:
        self.capability.assert_supports(AdapterOperation.ESTIMATE)
        return self._do_estimate(query)

    def discover_organizations(self, query: DiscoveryQuery) -> list[RawCandidate]:
        self.capability.assert_supports(AdapterOperation.DISCOVER_ORGANIZATIONS)
        return self._do_discover_organizations(query)

    def enrich_organization(self, org_ref: str) -> RawCandidate | None:
        self.capability.assert_supports(AdapterOperation.ENRICH_ORGANIZATION)
        return self._do_enrich_organization(org_ref)

    def enrich_contact(self, org_ref: str) -> list[RawCandidate]:
        # The personal-data operation: refused explicitly for an organization-only adapter.
        self.capability.assert_supports(AdapterOperation.ENRICH_CONTACT)
        return self._do_enrich_contact(org_ref)

    def detect_changes(self, cursor: str = "") -> ChangeSet:
        self.capability.assert_supports(AdapterOperation.DETECT_CHANGES)
        return self._do_detect_changes(cursor)

    def detect_deletions(self, cursor: str = "") -> ChangeSet:
        self.capability.assert_supports(AdapterOperation.DETECT_DELETIONS)
        return self._do_detect_deletions(cursor)

    # -- hooks a concrete adapter overrides (disabled future capability by default) -----------------

    def _do_estimate(self, query: DiscoveryQuery) -> CountEstimate:
        raise NotImplementedError(self._disabled(AdapterOperation.ESTIMATE))

    def _do_discover_organizations(self, query: DiscoveryQuery) -> list[RawCandidate]:
        raise NotImplementedError(self._disabled(AdapterOperation.DISCOVER_ORGANIZATIONS))

    def _do_enrich_organization(self, org_ref: str) -> RawCandidate | None:
        raise NotImplementedError(self._disabled(AdapterOperation.ENRICH_ORGANIZATION))

    def _do_enrich_contact(self, org_ref: str) -> list[RawCandidate]:
        raise NotImplementedError(self._disabled(AdapterOperation.ENRICH_CONTACT))

    def _do_detect_changes(self, cursor: str) -> ChangeSet:
        raise NotImplementedError(self._disabled(AdapterOperation.DETECT_CHANGES))

    def _do_detect_deletions(self, cursor: str) -> ChangeSet:
        raise NotImplementedError(self._disabled(AdapterOperation.DETECT_DELETIONS))

    @staticmethod
    def _disabled(operation: AdapterOperation) -> str:
        return (
            f"source-adapter operation '{operation.value}' is a disabled future capability "
            f"(DCA-123..127); concrete adapters are not implemented (CONCRETE_ADAPTERS_ENABLED="
            f"{CONCRETE_ADAPTERS_ENABLED})"
        )
