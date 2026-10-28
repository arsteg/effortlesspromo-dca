# DCA-086 — Prevent reacquisition of restricted identities

Stage: *Consent and restriction authority*. Scope: the marker that stops a restricted identity from
being silently **re-acquired**. DCA-084 erases a subject's operational data and DCA-085 propagates that
deletion outward — but neither stops the NEXT discovery cycle from fetching the same identity from the
same source and resurrecting it. This story closes that gap with **protected minimal tombstones** that
carry purpose/retention and a **scope-aware comparison** — and it deliberately does **not** call the
stored hashes anonymous.

## What was built (DCA only)

One new tenant-owned table (`restricted_identity_tombstones`), one migration (`0035`), one writer +
one pure comparison, a thin HTTP contract, and the discovery-cycle wiring. **No new scope** — recording
a tombstone reuses the SAME `data.erase` administrator authority that executes an erasure (DCA-084/085).

- **The pseudonym, never called anonymous** — `app/services/reacquisition.py::fingerprint_identity`
  is a deterministic SHA-256 over the normalized identifier (trim + case-fold) joined with the subject
  kind and the identifier scheme. The SAME identity hashes to the SAME value at restriction time and at
  reimport time (which is what makes the comparison work), and the raw identifier is **never stored**.
  Because the digest is *re-derivable from the identifier*, it is personal data in **pseudonymous**
  form, not anonymised data — the tombstone is tenant-owned (RLS), minimised, retained only for the
  declared purpose, never exported, and the audit trail records the tombstone's provenance **without**
  the fingerprint. The row records its `hash_algorithm` precisely so the value is an explicit,
  documented pseudonym rather than an opaque "anonymous" token.

- **The protected minimal tombstone** — `app/db/models.py::RestrictedIdentityTombstone` (tenant-owned,
  RLS). One row per restricted identity carrying only: the subject kind, the pseudonymous
  `identity_hash` + `hash_algorithm`, the sourcing `origin` (`RestrictedIdentityOrigin` — `platform` /
  `tenant`), the declared `purpose` (`ReacquisitionPurpose.prevent_reacquisition`), the `retention`
  (`until_lifted` / `until_date`, reusing the DCA-078 `RestrictionRetentionMode`), and opaque
  provenance (who/when/authority + the erasure/restriction it answers). Each `(subject_type, origin,
  identity_hash)` is **unique within a business**, so re-restricting the same identity is idempotent.

- **The writer** — `RestrictedIdentityTombstoneStore.record(...)`. Authority FIRST (a caller lacking
  `data.erase` is refused with `ReacquisitionAuthorizationError` before anything is read/written), then
  subject-type / origin / purpose / retention are validated and a non-empty identifier is required —
  each refusal raised before the session is mutated, so it stages NO row. The raw identifier is
  fingerprinted immediately and never stored; on success a tombstone + an atomic DCA-026 audit entry
  (opaque, *fingerprint-free*) are staged in the caller's unit of work. Idempotent: an existing
  tombstone for the same scope is returned unchanged.

- **The scope-aware comparison** — `reacquisition_decision(...)`: a pure, read-only function that
  fingerprints a candidate identity and returns `blocked` **only** when an ACTIVE tombstone matches the
  SAME business + subject kind + `origin` + fingerprint (stable reason `restricted_identity`). A
  different identity, a different origin (a tenant-supplied record vs a platform-sourced one), a lapsed
  `until_date` tombstone, or another tenant is NOT blocked — unrelated lawful scoped data is preserved.

- **Discovery wiring** — `app/services/discovery.py`: before a screened-in candidate is suggested, the
  cycle consults `reacquisition_decision` (organization identity, `platform` origin). A match screens
  the candidate out as `restricted_identity` (recorded as an `AcquisitionDecision`), taking precedence
  over the duplicate check so a restricted identity reads as restricted, not as a stale duplicate. The
  raw candidate is never persisted — a reimport cannot resurrect it.

- **Routes** `app/api/routes/reacquisition.py` (mounted in `app/main.py`):
  - `POST /v1/tenants/{business_id}/restricted-identities` — record a tombstone (403 without
    `data.erase`; 422 on a bad subject type / origin / purpose / retention / missing identifier;
    idempotency-key safe). The raw identifier is fingerprinted and never stored.
  - `GET /v1/tenants/{business_id}/restricted-identities` — read the ledger (`data.erase`; the
    projection carries the pseudonymous fingerprint + provenance, never a raw identifier).

### Acceptance criteria → where they hold

- *Reimport cannot resurrect a restricted platform-sourced record; unrelated lawful scoped data is
  preserved* — `test_reimport_cannot_resurrect_restricted_identity`: a first discovery suggests the
  identity; after a tombstone is recorded (and the candidate deleted, simulating the erasure) a reimport
  from the same source is screened out `restricted_identity` and NO candidate is recreated, while the
  unrelated candidate stays present (duplicate). `test_reimport_block_is_tenant_scoped`: another tenant
  that never restricted it may still lawfully acquire the same identity.
- *Reproduce the result and relevant denial without unintended state changes or later-story work* — the
  writer checks authority before anything is read/written, so the viewer / ordinary-user denials
  (`test_record_denied_for_viewer_changes_nothing`, `test_record_denied_for_ordinary_user`) stage
  nothing; a re-restriction is idempotent (`test_record_is_idempotent`); the comparison is pure (no
  state change) and scope-aware (`test_decision_preserves_unrelated_identity_origin_and_tenant`,
  `test_decision_until_date_tombstone_lapses`). No send, no model call.

## Tests / quality

- `tests/test_reacquisition_tombstone.py` — service units (SQLite): fingerprint-only storage,
  fingerprint-free audit, idempotency, authority denials, and the scope-aware comparison (identity /
  origin / tenant / lapsed). Discovery integration: reimport cannot resurrect, unrelated preserved,
  tenant-scoped. HTTP (`TestClient`): admin records + reads (no raw identifier in the projection),
  viewer 403, cross-tenant 403, bad body 422. Postgres-backed (`DCA_TEST_DATABASE_URL`): idempotency
  under the unique constraint + the comparison on real transactions.
- `tests/test_migrations.py` + `tests/test_rls.py` updated: `restricted_identity_tombstones` added to
  the covered set (a `0035` create branch; tenant-owned → tenant-isolation RLS).
- Quality gate: `ruff check .` · `ruff format --check .` · `mypy` · `pytest` (all green for the changed
  files; the full suite passes). Note: a few pre-existing `ruff`/`mypy` findings in files this story did
  NOT touch (`app/services/policy_decision_record.py`, `app/services/field_correction.py`,
  `app/compliance/policy_rulepacks.py`, unrelated `scripts/manual_check.py` scenarios) are left as-is.

## Manual check

```text
python scripts/manual_check.py DCA-086
```

In isolated synthetic Businesses A/B: runs discovery in A (the identity is suggested), records a
tombstone for it (and deletes its candidate row, simulating the erasure), reimports from the same source
(screened out `restricted_identity`, NOT resurrected; the unrelated candidate preserved), shows Business
B — which never restricted it — may still lawfully acquire the same identity (scope-aware, tenant-
isolated), shows the tombstone stores only the pseudonymous fingerprint (never the raw identifier), and
shows a read-only viewer denied with nothing written. Local synthetic only, no AI/paid action.

## Boundary

Python DCA only (backend + migration + tests + docs). No `.NET`, no AI-service, no Web/Admin changes. No
new provider SDK/key, model routing or second gateway. No new scope. Lifting a tombstone (re-permitting
reacquisition) and any live source stay disabled future capabilities.
