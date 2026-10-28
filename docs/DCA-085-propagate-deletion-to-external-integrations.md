# DCA-085 — Propagate deletion to external integrations

Stage: *Consent and restriction authority*. Scope: after DCA-084 erased the DCA's OWN operational
data (and journaled it), **propagate that deletion outward** — send an **idempotent** deletion/removal
request through each supported external platform/provider contract and **record the outcome per
destination** (an acknowledgment, an explicit unsupported state, a pending-reconciliation timeout, or a
failure). DCA-084 deleted locally; this story reaches the external copies.

## What was built (DCA only)

One contract + a synthetic fake + a single sanctioned writer + its receipt table + a thin HTTP
contract. One new tenant-owned table (`external_deletion_propagations`), one migration (`0034`). **No
new scope** — propagation uses the SAME `data.erase` administrator authority that executed the erasure.

- **Contract / port** `app/integration/deletion.py` — `DeletionDestinationClient` Protocol
  (`supported_destinations()` + `dispatch(request)`), opaque-only `DeletionDispatchRequest`
  (business/journal/subject ids + the controlled destination key + a **deterministic** idempotency
  key), `DeletionResponse` (`ACKNOWLEDGED` / `UNSUPPORTED`), and the two failure signals:
  `DeletionTimeout` (→ recorded PENDING) and `DeletionRejected` (→ recorded FAILED).
  `build_idempotency_key(journal_id, destination)` is deterministic so a retry/reconcile re-send
  carries the SAME key (de-duplicated by the destination, exactly once end to end).
- **Synthetic fake** `app/integration/deletion_fake.py::FakeDeletionDestinationClient` — the ONLY
  implementation today (deterministic, no network, no wall clock, every response `synthetic`).
  Per-destination behaviours `ack` / `unsupported` / `timeout` / `reject`; a `timeout` destination can
  recover on a re-send (the reconciliation seam). `build_deletion_destination_client(settings)` returns
  **`None` in production** — the live adapter is unbuilt, so the contract stays disabled and a
  propagation fails closed rather than acknowledging against a synthetic destination.
- **Writer** `app/services/deletion_propagation.py::DeletionPropagator`:
  - `propagate(journal, principal, *, destinations=None, now=None)` — authority FIRST
    (`DeletionPropagationAuthorizationError` before anything is read/sent/written); then for each
    supported destination: a destination already `acknowledged`/`unsupported` (terminal) is **reused**
    (no re-send, no rewrite — idempotent); otherwise an idempotent request is sent and the outcome
    recorded with an atomic DCA-026 audit entry. **A timeout → `pending` (pending reconciliation), NEVER
    acknowledged/completed.**
  - `reconcile(principal, *, journal_id=None, now=None)` — re-sends the SAME idempotent request for
    every `pending` destination; a recovered destination advances to `acknowledged`, one still
    unavailable stays `pending` (attempts incremented).
  - Stages into the caller's session; performs no DB I/O of its own (the destination call is the
    external contract's I/O, via the injected port). Every reference recorded is opaque.
- **Table** `external_deletion_propagations` (tenant-owned, RLS) — one row per `(erasure journal,
  destination)`, unique within a business (`uq_external_deletion_journal_destination`), carrying the
  status, opaque ack ref, reason code, attempt count, who/when. No personal data.
- **Routes** `app/api/routes/deletion_propagation.py` (mounted in `app/main.py`):
  - `POST /v1/tenants/{business_id}/erasure-journal/{journal_id}/external-deletions` — propagate (403
    without `data.erase`; 404 unknown/cross-tenant journal; 503 when the contract is disabled;
    idempotency-key safe).
  - `POST .../external-deletions/reconcile` — reconcile this journal's pending destinations.
  - `GET  .../external-deletions` — read the per-destination receipts (`data.erase`; opaque only).

## Acceptance evidence

- **A timeout is pending reconciliation, not a completed deletion** — `test_timeout_is_pending_not_completed`
  (status `pending`, never `acknowledged`, no ack ref, `is_complete` False); the manual-check shows the
  same, then a reconcile advancing it to `acknowledged` on a re-send of the SAME idempotency key.
- **Reproducible result + relevant denial, no unintended state** — a re-run is an idempotent no-op
  (`test_propagate_is_idempotent_terminal_not_resent`, one row per destination, terminal destinations
  not re-sent); a viewer/ordinary user is denied with nothing sent and nothing written
  (`test_propagate_denied_for_viewer_changes_nothing`, `test_propagate_denied_for_ordinary_user`);
  another tenant is untouched; a disabled contract fails closed (503).

## Tests

`tests/test_deletion_propagation.py` — service unit (SQLite), HTTP (`TestClient`), and a Postgres-backed
test (`DCA_TEST_DATABASE_URL`) exercising the unique constraint + reconcile on real transactions.
`tests/test_rls.py` expected tenant-owned set updated to include the new table.

Manual check: `python scripts/manual_check.py DCA-085` (local synthetic, no AI/paid/network).

## Boundary

DCA only (backend + migration + tests + docs). No AI-service, Web, Admin or other-service changes. The
live destination adapter stays disabled (production has no client); future capabilities remain off.
