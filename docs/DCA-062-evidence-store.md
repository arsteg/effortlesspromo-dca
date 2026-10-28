# DCA-062 — Store only permitted evidence objects

Stage: Evidence and identity resolution · key `evidence-store` · source sections 6, 7.

## What this adds

Bounded evidence storage with **license-aware retention** and **tenant-scoped object access**. When a
source's licence prohibits keeping raw content, the service preserves a **reference** (the locator + a
non-reversible digest) instead of the raw payload.

- Model `EvidenceObject` (`app/db/models.py`) — tenant-owned (`business_id`), RLS-protected.
- Store `EvidenceObjectStore` (`app/services/evidence_store.py`) — the single sanctioned writer.
- Migration `0027_evidence_objects` — dual-path safe create branch + RLS policy (fresh DBs get the
  table from the 0001 metadata baseline and RLS from 0002, so 0027 is a no-op there).

No HTTP route, no AI capability, no acquisition/send path — capturing evidence grants nothing; future
capabilities stay disabled. No .NET / UI / AI-service changes.

## License-aware retention (derived, never caller-chosen)

`decide_retention(terms, content_kind)` reads the cited `SourceTerms` (DCA-045) version:

- **`raw_retained`** — the terms GRANT the `store` operation (per-field override wins over the
  source-wide default) AND the retention policy permits raw retention. The raw payload is kept,
  bounded by `retention_until = captured_at + max_days` when the terms set a window.
- **`reference_only`** — raw retention is prohibited; `raw_content` is left `NULL` and never written.
  Only `content_ref` (locator) and `content_digest` (SHA-256 of the captured bytes) are kept. The
  recorded `retention_reason`:
  - `store_not_permitted` — the `store` right is withheld.
  - `no_retention_policy` — the retention policy is a named no-retention policy
    (`no_retention` / `none` / `no_store` / `do_not_store` / `reference_only` / `metadata_only`, …).
  - `zero_retention_window` — `retention.max_days <= 0`.

## Acceptance criteria → evidence

- **A no-retention source leaves no raw content copy** — `reference_only` objects store `raw_content =
  NULL`; the locator + digest survive. (`test_no_retention_source_keeps_only_a_reference_not_raw`,
  parametrized over all three prohibition reasons.)
- **Another tenant cannot retrieve an evidence object by ID** — `get()` is business-scoped; a
  cross-tenant id reads as not-found. (`test_tenant_isolation_b_cannot_read_a_evidence_object`,
  `test_cross_tenant_subject_is_rejected_and_stages_nothing`.) RLS enforces the same at the DB.
- **Reproduce without unintended state changes** — every refusal stages no row
  (`test_each_missing_or_invalid_piece_is_rejected`); each capture stages an atomic DCA-026 audit entry
  whose references never carry the raw payload or locator
  (`test_each_store_is_atomically_audited_without_the_raw_payload`); no prospect/host record is created
  (`test_no_prospect_or_host_record_is_created`).

## Run

```text
# focused unit tests (in-memory SQLite, no network/Docker)
pytest tests/test_evidence_store.py

# inspectable scenario (local synthetic, ephemeral DB, AI disabled)
python scripts/manual_check.py DCA-062

# real transaction / RLS path on a disposable PostgreSQL (NOT SQLite)
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
pytest tests/test_evidence_store.py tests/test_migrations.py tests/test_rls.py

# quality gate
ruff check . && ruff format --check . && mypy && pytest
```
