# DCA-122 — Define Python source adapter capabilities

Stage: Approved source access · Key: `source-protocol` · Source sections 6, 8, 9

## What this adds

The **typed contract every concrete source adapter (DCA-123..127) will declare and implement** — and
nothing more. There is no live adapter, no fetch, and no enabled source. Three pure things:

- `app/sources/capabilities.py`:
  - `AdapterOperation` — the operations an adapter may declare it supports: `LIST_CAPABILITIES`
    (mandatory, rights-free), `ESTIMATE` (count/size hint, no retrieval), `DISCOVER_ORGANIZATIONS`,
    `ENRICH_ORGANIZATION`, `ENRICH_CONTACT` (the **only** personal-data operation),
    `DETECT_CHANGES`, `DETECT_DELETIONS`.
  - `REQUIRED_RIGHTS` — the minimal `SourceOperation` rights (DCA-045) each operation depends on
    (record-retrieving ops need `COLLECT`; listing/estimation need none).
  - `SourceAdapterCapability` — an immutable descriptor an adapter publishes: `key`, `source_kind` /
    `access_method` (reusing the DCA-044 catalog enums), the supported `operations`, the
    `required_rights` it declares, and `handles_personal_contacts`. Helpers: `supports` /
    `assert_supports`, `is_organization_only`, `minimal_rights_for`, and
    `permitted_operations(granted_rights)` (the declared operations a given rights grant would permit).
  - `validate_capability(cap)` — raises `CapabilityValidationError` (stable code) for an incoherent
    declaration, before anything is stored (mirrors the DCA-044 source-catalog store).
  - `CapabilityRegistry` — validates on `register`, rejects duplicates, and `all()` / `keys()` /
    `require()` list what is registered. Empty in the running service (no adapter registers itself).
  - `SourceAdapter` (typed `Protocol`) + `BaseSourceAdapter` (ABC) — the surface a concrete adapter
    implements. The ABC gates **every** operation through `assert_supports` and leaves the `_do_*`
    hooks raising `NotImplementedError`, so no live adapter exists yet.
  - Typed shapes `DiscoveryQuery` / `CountEstimate` / `ChangeSet` (definitions only).
  - `CONCRETE_ADAPTERS_ENABLED = False`.

No migration: these are pure definitions, no schema.

## The guarantees (acceptance criteria)

- **An unsupported capability fails EXPLICITLY — never a silent empty result.** Every operation call
  passes through `capability.assert_supports(...)`; an undeclared operation raises
  `UnsupportedCapabilityError` (code `operation_not_supported`), so a caller distinguishes
  "no results" from "not supported".
- **An organization-only adapter can never silently fetch personal contacts.** `ENRICH_CONTACT` is the
  only personal operation; a descriptor may declare it ONLY if it also sets `handles_personal_contacts`
  (and declares the `COLLECT` right). `validate_capability` rejects an org-only descriptor that claims
  contact enrichment (`personal_contact_not_declared`) before it can register, and the ABC still
  refuses the call at runtime — no declared and no executed path to contacts.
- **An adapter cannot claim an operation it is not entitled for.** A descriptor supporting an operation
  without declaring the right that operation needs is rejected (`required_rights_not_declared`).
  `permitted_operations` then intersects the declared operations with the rights the DCA-044..048 chain
  actually grants, so an operation whose right is withheld simply is not permitted.
- Reproducible with no state change: validation reads and changes nothing; a rejected declaration
  leaves the registry untouched.

## Nothing fetches

The descriptor/Protocol/registry are declarations. `CONCRETE_ADAPTERS_ENABLED` is `False`, every
`_do_*` hook raises, and no adapter registers in the running service. The concrete adapters
(DCA-123..127) and any fetch stay disabled future capabilities. This composes with — never bypasses —
the source-rights chain (DCA-044..048) and the fetch guards (DCA-118..121).

## Tests / manual check

- `tests/test_source_adapter_capabilities.py` — coherent descriptors validate; the org-only /
  dangling-personal / over-claim / empty-ops / missing-listing cases are each rejected with their
  stable code; `permitted_operations` tracks granted rights; the registry lists / dedups / requires and
  stores nothing on a rejection; and a synthetic org-only adapter built on the ABC runs a supported
  operation but refuses contact enrichment explicitly. No network, no socket, no fetch.
- `python scripts/manual_check.py DCA-122` — prints the two registered capabilities (operations +
  required rights + org-only flag), a rejected invalid declaration (org-only claiming contact
  enrichment, registry size unchanged), and a synthetic org-only adapter refusing `enrich_contact`
  explicitly at runtime while discovery returns a fixture.

## Boundary

Python DCA only; no AI, no network, no other service's database, no .NET. No migration. Definitions +
focused tests + this doc. Every concrete adapter and any actual fetching stay disabled.
