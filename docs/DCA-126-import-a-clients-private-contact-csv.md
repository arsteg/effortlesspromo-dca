# DCA-126 — Import a client's private contact CSV

Stage: Approved source access · Key: `csv-private-import` · Source sections 6, 8, 9

## What this adds

The **fourth** concrete source adapter behind the DCA-122 contract, and the **FIRST that writes PERSONAL
data**. The first three adapters were organization-only (DCA-123 dataset import, DCA-124 website extract,
DCA-125 licensed provider — all refused any personal contact field). This adapter imports a **client's
OWN private contact list** — personal (person + contact-point) data the business already holds a lawful
basis for — into the tenant-private DCA-060 `PersonObservation` + `ContactPoint` store.

Because it legitimately handles personal data, its capability descriptor
(`PRIVATE_CONTACT_IMPORT_CAPABILITY`) is the first to declare `handles_personal_contacts=True` and the
only personal-data operation, `ENRICH_CONTACT` — so this is also the first proof that DCA-122's
personal-contact path is coherent (it is still validated at import time).

There is **no network fetch**: the CSV is **caller-supplied** (already-parsed rows). The adapter adds
column mapping + validation and a bounded, deterministic private import path.

## The load-bearing guarantees (acceptance criteria)

1. **Imported contact details stay tenant-private.** Every person / contact point is written through the
   DCA-060 store bound to one verified business scope; there is no global index on a contact value, so
   one business's imported CSV is **never** visible to another (DCA-017 scoping + DCA-018 RLS). The same
   value imported by two businesses stays two independent private rows.
2. **Importing a contact is NOT recipient consent.** The import records the contact plus the
   **client-asserted lawful `basis`** (`ContactImportBasis` — the explicit permission-EVIDENCE status)
   and provenance. It **never** fabricates a `ConsentReceipt`, never clears suppression, and keeps the
   send pipeline a disabled future capability. A `subscribed`-style column is **recorded as a client
   claim only and never trusted as consent** ("do not trust a subscribed flag").
3. **A suppressed / restricted contact is EXCLUDED or flagged.** Each parsed contact point is gated
   through the DCA-079 authoritative suppression lookup **and** the DCA-078 local processing
   restrictions. A value on the host suppression list, or whose channel/purpose is under an in-force
   restriction (or a duplicate within the business), is **not stored** as a contactable `ContactPoint` —
   it is excluded and only **flagged** (opaque channel + reason) on the provenance. No contactable record
   for a suppressed/restricted value.
4. **Invalid rows have safe row-level errors and no fabricated consent.** A row missing a name, with no
   contact column, or whose only contact value is malformed, is refused with a stable per-row code and
   imports nothing (no person, no contact, no consent). Other rows still import.
5. **Idempotent re-import.** `(business_id, dataset_id, source_row_ref)` is the idempotency key: a row
   already imported for this list is skipped (its existing person reused), so a re-run is a deterministic
   no-op.

## Why storing ≠ contacting (the suppression stance)

Only a value the host authoritatively reports **suppressed** on the channel is excluded. Other
suppression outcomes (registry unavailable / stale / channel-not-covered) do **not** block the import:
storing a first-party contact is not contacting it, and the send pipeline re-checks suppression
authoritatively before anything could ever be sent. Restrictions are the tenant's own explicit
instruction and always exclude the covered channel/purpose.

## Files

| File | Change |
|---|---|
| `app/db/models.py` | Added `SourceKind.FIRST_PARTY` (honest kind for client-supplied data); new `ContactImport` tenant-owned provenance table + `ContactImportBasis` / `ContactImportStatus` enums. |
| `app/sources/private_contact_import.py` | **New.** `PrivateContactImporter`, `PrivateImportManifest`, `ColumnMapping`, result shapes, the suppression/restriction gates and format validation. |
| `migrations/versions/0052_contact_imports.py` | **New.** Dual-path (create + RLS) migration for `contact_imports`, revises `0051`. |
| `tests/test_private_contact_import.py` | **New.** Focused tests (valid import, suppressed/restricted exclusion, idempotency, tenant isolation, malformed-row rejection, Postgres path). |
| `tests/test_rls.py` | Added `contact_imports` to the exact tenant-owned RLS set. |
| `tests/test_migrations.py` | Added `contact_imports` to `DCA_TABLES` + a 0052 create-branch test. |
| `scripts/manual_check.py` | Registered the `DCA-126` inspectable scenario. |

## Data model — `contact_imports`

One tenant-owned row per imported CSV row: `person_observation_id`, the opaque `dataset_id` batch, the
client's `source_label` / `source_version`, the checkpointable `source_row_ref`, the client-asserted
`basis` + `purpose`, the **untrusted** `asserted_subscribed` claim, the per-row `status`
(`imported` / `partially_excluded` / `excluded`), the `contacts_imported` / `contacts_excluded` counts
and opaque `exclusion_flags` (`[channel, reason]` — **never a raw contact value**). Unique on
`(business_id, dataset_id, source_row_ref)`.

## What stays disabled

Importing authorizes **nothing**. No `ConsentReceipt` is created from a basis or a subscribed flag;
suppression is never cleared; no value is promoted to any shared/global table; and the outbound send
pipeline remains a disabled future capability. The basis is a recorded assertion for review only.

## Verify

```text
.venv/Scripts/python.exe -m pytest tests/test_private_contact_import.py -q
.venv/Scripts/python.exe scripts/manual_check.py DCA-126
```

The disposable-Postgres paths (real transactions + RLS) run when `DCA_TEST_DATABASE_URL` is set; they
skip otherwise.
