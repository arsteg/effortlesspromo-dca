"""DCA-122 — the source-adapter capability contract (descriptor + Protocol/ABC + registry).

Pure definitions, no fetch: a coherent descriptor validates and registers; an incoherent / over-claiming
descriptor is rejected with a stable code; the registry lists what is registered; and the load-bearing
guarantees hold — an unsupported capability fails EXPLICITLY, and an organization-only adapter can never
declare OR perform contact enrichment. Nothing here opens a socket or fetches anything.
"""

from __future__ import annotations

import pytest

from app.compliance.rulepacks import RawCandidate
from app.db.models import SourceAccessMethod, SourceKind, SourceOperation
from app.sources.capabilities import (
    CONCRETE_ADAPTERS_ENABLED,
    REASON_LISTING_REQUIRED,
    REASON_NO_OPERATIONS,
    REASON_PERSONAL_CONTACT_NOT_DECLARED,
    REASON_PERSONAL_WITHOUT_CONTACT_OP,
    REASON_RIGHTS_NOT_DECLARED,
    AdapterOperation,
    BaseSourceAdapter,
    CapabilityRegistry,
    CapabilityValidationError,
    ChangeSet,
    CountEstimate,
    DiscoveryQuery,
    SourceAdapter,
    SourceAdapterCapability,
    UnknownAdapterError,
    UnsupportedCapabilityError,
    validate_capability,
)

# --- synthetic descriptors (declarations only; no adapter, no fetch) ------------------------------

ORG_ONLY = SourceAdapterCapability(
    key="synthetic.org_registry",
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
)

WITH_CONTACTS = SourceAdapterCapability(
    key="synthetic.licensed_provider",
    source_kind=SourceKind.COMMERCIAL,
    access_method=SourceAccessMethod.API,
    operations=frozenset(
        {
            AdapterOperation.LIST_CAPABILITIES,
            AdapterOperation.DISCOVER_ORGANIZATIONS,
            AdapterOperation.ENRICH_ORGANIZATION,
            AdapterOperation.ENRICH_CONTACT,
        }
    ),
    required_rights=frozenset({SourceOperation.COLLECT}),
    handles_personal_contacts=True,
    trust_tier="licensed",
)


# --- validation: coherent descriptors pass --------------------------------------------------------


def test_valid_org_only_descriptor_validates():
    assert validate_capability(ORG_ONLY) is ORG_ONLY
    assert ORG_ONLY.is_organization_only is True


def test_valid_contact_descriptor_validates():
    assert validate_capability(WITH_CONTACTS) is WITH_CONTACTS
    assert WITH_CONTACTS.is_organization_only is False


# --- validation: incoherent / over-claiming descriptors are rejected ------------------------------


def test_org_only_adapter_cannot_claim_contact_enrichment():
    # The headline acceptance criterion: an org-only adapter claiming the personal operation is refused.
    bad = SourceAdapterCapability(
        key="synthetic.sneaky",
        source_kind=SourceKind.OFFICIAL,
        access_method=SourceAccessMethod.BULK_DOWNLOAD,
        operations=frozenset(
            {AdapterOperation.LIST_CAPABILITIES, AdapterOperation.ENRICH_CONTACT}
        ),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=False,
    )
    with pytest.raises(CapabilityValidationError) as exc:
        validate_capability(bad)
    assert exc.value.code == REASON_PERSONAL_CONTACT_NOT_DECLARED


def test_personal_flag_without_contact_operation_is_rejected():
    bad = SourceAdapterCapability(
        key="synthetic.dangling_personal",
        source_kind=SourceKind.COMMERCIAL,
        access_method=SourceAccessMethod.API,
        operations=frozenset(
            {AdapterOperation.LIST_CAPABILITIES, AdapterOperation.DISCOVER_ORGANIZATIONS}
        ),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=True,
    )
    with pytest.raises(CapabilityValidationError) as exc:
        validate_capability(bad)
    assert exc.value.code == REASON_PERSONAL_WITHOUT_CONTACT_OP


def test_overclaiming_operation_without_declared_right_is_rejected():
    # Supports DISCOVER (needs COLLECT) but declares no rights at all -> over-claim.
    bad = SourceAdapterCapability(
        key="synthetic.overclaim",
        source_kind=SourceKind.OFFICIAL,
        access_method=SourceAccessMethod.API,
        operations=frozenset(
            {AdapterOperation.LIST_CAPABILITIES, AdapterOperation.DISCOVER_ORGANIZATIONS}
        ),
        required_rights=frozenset(),
    )
    with pytest.raises(CapabilityValidationError) as exc:
        validate_capability(bad)
    assert exc.value.code == REASON_RIGHTS_NOT_DECLARED
    assert SourceOperation.COLLECT.value in (exc.value.detail or "")


def test_empty_operations_and_missing_listing_are_rejected():
    empty = SourceAdapterCapability(
        key="synthetic.empty",
        source_kind=SourceKind.OFFICIAL,
        access_method=SourceAccessMethod.API,
        operations=frozenset(),
    )
    with pytest.raises(CapabilityValidationError) as exc:
        validate_capability(empty)
    assert exc.value.code == REASON_NO_OPERATIONS

    no_listing = SourceAdapterCapability(
        key="synthetic.no_listing",
        source_kind=SourceKind.OFFICIAL,
        access_method=SourceAccessMethod.API,
        operations=frozenset({AdapterOperation.ESTIMATE}),
    )
    with pytest.raises(CapabilityValidationError) as exc2:
        validate_capability(no_listing)
    assert exc2.value.code == REASON_LISTING_REQUIRED


# --- permitted_operations: cannot do what it is not entitled for ----------------------------------


def test_permitted_operations_tracks_granted_rights():
    # With COLLECT granted, every declared op (all need at most COLLECT) is permitted.
    assert ORG_ONLY.permitted_operations({SourceOperation.COLLECT}) == ORG_ONLY.operations
    # With NO rights granted, only the rights-free operations remain.
    permitted = ORG_ONLY.permitted_operations(set())
    assert permitted == frozenset(
        {AdapterOperation.LIST_CAPABILITIES, AdapterOperation.ESTIMATE}
    )
    assert AdapterOperation.DISCOVER_ORGANIZATIONS not in permitted


# --- registry: validates, lists, dedups, requires -------------------------------------------------


def test_registry_lists_registered_capabilities():
    registry = CapabilityRegistry()
    assert len(registry) == 0  # nothing registered in the running service
    registry.register(ORG_ONLY)
    registry.register(WITH_CONTACTS)

    assert registry.keys() == ["synthetic.licensed_provider", "synthetic.org_registry"]
    assert [c.key for c in registry.all()] == registry.keys()
    assert "synthetic.org_registry" in registry
    assert registry.require("synthetic.org_registry") is ORG_ONLY
    assert registry.get("nope") is None


def test_registry_rejects_invalid_before_storing():
    registry = CapabilityRegistry()
    bad = SourceAdapterCapability(
        key="synthetic.bad",
        source_kind=SourceKind.OFFICIAL,
        access_method=SourceAccessMethod.API,
        operations=frozenset({AdapterOperation.LIST_CAPABILITIES, AdapterOperation.ENRICH_CONTACT}),
        required_rights=frozenset({SourceOperation.COLLECT}),
        handles_personal_contacts=False,
    )
    with pytest.raises(CapabilityValidationError):
        registry.register(bad)
    assert len(registry) == 0  # refusal stored nothing


def test_registry_rejects_duplicate_key():
    registry = CapabilityRegistry()
    registry.register(ORG_ONLY)
    with pytest.raises(CapabilityValidationError):
        registry.register(ORG_ONLY)
    with pytest.raises(UnknownAdapterError):
        registry.require("missing")


# --- the ABC: unsupported capabilities fail EXPLICITLY, personal contact refused at runtime --------


class _FakeOrgOnlyAdapter(BaseSourceAdapter):
    """A test-only org-only adapter: supports listing + discovery, refuses everything else.

    It implements ONLY the discovery hook with an obviously-synthetic record; it declares NO personal
    operation, so contact enrichment must fail explicitly (never a silent empty list).
    """

    @property
    def capability(self) -> SourceAdapterCapability:
        return ORG_ONLY

    def _do_discover_organizations(self, query: DiscoveryQuery) -> list[RawCandidate]:
        return [
            RawCandidate(
                org_name="Synthetic Org Pvt Ltd",
                org_identifier="U00000MH2026PTC900122",
                source_key=self.capability.key,
                source_ref="synthetic://org/900122",
                trust_tier=self.capability.trust_tier,
                contact={"kind": "role_email", "email": "info@synthetic.example"},
            )
        ]


def test_fake_adapter_satisfies_protocol():
    assert isinstance(_FakeOrgOnlyAdapter(), SourceAdapter)


def test_supported_operation_runs_through_the_gate():
    adapter = _FakeOrgOnlyAdapter()
    out = adapter.discover_organizations(DiscoveryQuery(industries=("textiles",)))
    assert len(out) == 1 and out[0].org_name == "Synthetic Org Pvt Ltd"


def test_org_only_adapter_refuses_contact_enrichment_explicitly():
    adapter = _FakeOrgOnlyAdapter()
    with pytest.raises(UnsupportedCapabilityError) as exc:
        adapter.enrich_contact("org-ref")
    assert exc.value.operation is AdapterOperation.ENRICH_CONTACT
    assert exc.value.adapter_key == ORG_ONLY.key


def test_unimplemented_but_declared_op_is_a_disabled_future_capability():
    # ESTIMATE is declared but the fake provides no hook -> the ABC default marks it disabled, NOT a
    # silent result. (No fetch exists; concrete adapters are disabled.)
    adapter = _FakeOrgOnlyAdapter()
    with pytest.raises(NotImplementedError):
        adapter.estimate(DiscoveryQuery())
    assert CONCRETE_ADAPTERS_ENABLED is False


def test_shapes_are_inert_value_objects():
    # The typed shapes are pure definitions with no behaviour / no fetch.
    assert CountEstimate(approximate_total=5).approximate_total == 5
    assert ChangeSet().changed == () and ChangeSet().deleted_refs == ()
