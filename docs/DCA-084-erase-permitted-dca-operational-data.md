# DCA-084 — Erase permitted DCA operational data

Stage: *Consent and restriction authority*. Scope: the **first executor that actually deletes** — a
single, scoped deletion of a subject's DCA-owned OPERATIONAL data (observations, evidence, projections)
with an **execution journal** holding **justified tombstone references**. DCA-082 only recorded a
request and DCA-083 only *identified* a scope (both executed nothing); this story erases.

## What was built (DCA only)

A single sanctioned executor + its journal table + a thin HTTP contract. One new tenant-owned table
(`data_erasure_journals`), one migration (`0033`), one new business-plane scope (`data.erase`).

- **Scope** `app/services/data_erasure.py::OperationalDataEraser` — `erase_subject(subject_type,
  subject_id, principal, *, justification, request_ref="", request_kind=...)`. For ONE of a business's
  subjects (an org or person observation) it hard-deletes, WITHIN that business only:
  - **observations** — the identity node (`OrgObservation`/`PersonObservation`), its external-id
    mappings (`OrgExternalId`, org) or contact points (`ContactPoint`, person), and every
    `FieldObservation` about it;
  - **evidence** — every `EvidenceObject` about the subject;
  - **projections** — the tenant's canonical correction overlay (`FieldCorrection`) about the subject.
  It then stages a `DataErasureJournal` row (opaque tombstone row-ids grouped by table + counts + who/
  when/why/authority) and an atomic DCA-026 audit entry, in the caller's unit of work.
- **Permitted boundary (what is NOT erased):** the records a privacy obligation requires be RETAINED —
  the DCA-026 audit trail, DCA-074 consent receipts, DCA-078 restriction ledgers — and any
  platform-owned/control-plane data. Child person observations of an erased org are separate subjects
  (erased by their own request); the executor never cascades beyond the named subject. Acting beyond
  this scope stays a disabled future capability.
- **Authority** — `erase_subject` requires a business administrator holding the new business-plane
  `data.erase` scope (admin/owner only). An ordinary acting user, a read-only viewer, and a platform
  operator (who holds no business membership) are all refused. The check runs BEFORE anything is read or
  deleted, so a relevant denial deletes nothing and writes no journal. (A platform operator erasing a
  client's data would need a separate scoped/expiring client-support grant — a later story — never an
  Admin-role-alone right.)
- **Routes** `app/api/routes/privacy_erasure.py` (mounted in `app/main.py`):
  - `POST /v1/tenants/{business_id}/subjects/{subject_type}/{subject_id}/erasure` — execute (403 without
    `data.erase`; 404 unknown/cross-tenant subject; 422 missing justification / unknown subject type /
    justification carrying raw contact; idempotency-key safe).
  - `GET /v1/tenants/{business_id}/erasure-journal` and `/{journal_id}` — read the durable journal
    (`data.erase`; cross-tenant/unknown id → 404).

### Acceptance criteria → where they hold

- *Erased operational data is no longer accessible; audit retains only permitted minimal metadata* — the
  observation/evidence/projection rows are hard-deleted, so no tenant-scoped read returns them. What
  remains is the journal row + the audit entry, both carrying only OPAQUE references (row ids, counts,
  opaque subject id, a justification code) — screened by the DCA-026 audit sink, so a contact value /
  field value can never land there. The retained records (audit/consent/restriction) are untouched.
- *Reproduce the result and relevant denial without unintended state changes or later-story work* — the
  executor checks authority and resolves the subject before deleting, so the viewer/ordinary-user denial
  (authorization error), the unknown/cross-tenant subject (not-found) and the missing justification each
  change nothing; a re-run of an already-erased subject reads as not-found (idempotent). No send, model
  call, or acting beyond the permitted scope.

## Tests / quality

- `tests/test_data_erasure.py` — service unit (SQLite): full-delete + journal/tombstones, PII-free
  audit metadata, tenant-scoping (B's identical subject untouched), retained-records survive, and the
  denials (viewer / ordinary user / unknown / cross-tenant / missing justification / unknown type /
  idempotent re-run) each change nothing. HTTP (`TestClient`): admin erases, viewer 403, cross-tenant
  404, contact-bearing justification 422 (nothing deleted). Postgres-backed (`DCA_TEST_DATABASE_URL`):
  rows gone, journal + audit remain on real transactions.
- `tests/test_migrations.py` + `tests/test_rls.py` updated: `data_erasure_journals` added to the covered
  set, with a `0033` create-branch + RLS test (tenant-owned → tenant-isolation policy).
- Quality gate: `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```text
python scripts/manual_check.py DCA-084
```

Seeds a synthetic person subject (identity + contact point + field observation + evidence + a correction
projection) into Business A AND the SAME-shaped subject into Business B, has an A administrator execute
the erasure (A's rows deleted + journaled with opaque tombstone references), then shows B is untouched,
a read-only viewer is denied with nothing deleted, and the journal/audit retain only opaque minimal
metadata (no contact/field value) — local synthetic only, no AI/paid action.

## Boundary

Python DCA only. No `.NET`, no AI-service, no Web/Admin changes. No new provider SDK/key, model routing
or second gateway. The erasure is scoped to the DCA's OWN operational observation/evidence/projection
data; retained records and acting beyond this scope stay out of scope / disabled future capabilities.
