# DCA-156 — Reconcile uncertain audience synchronization

Stage: User selection and EffortlessPromo sync · Key: `sync-reconcile` · Status: **Ready for manual review**

## What this adds

The DCA-155 sync drives one accepted record through the DCA-154 audience-sync port. When the platform's
acknowledgment is **lost / timed-out / returned PENDING**, that sync leaves the DCA-153
`AcceptanceIntent` in an **in-doubt** state — `status='pending_sync'`, `sync_status='pending'` — and
nobody knows whether the CRM record was actually written.

DCA-156 resolves that one ambiguity with a single tenant-scoped service,
`UncertainAudienceSyncReconciler` (`app/services/acceptance_sync_reconcile.py`), mirroring the DCA-116
uncertain-provider-outcome resolver and the DCA-155 synchronizer:

- **Re-drive, don't re-create.** The reconciler RE-DRIVES the DCA-154 port under the **same stable
  request key** the intent was staged under (`audience_sync_request_from_intent`). External ids are
  deterministic in that key, so the platform's receipt/status lookup can only ever return the **same**
  external record — a retry can **never** create a second CRM record / a duplicate audience member.
- **Only an ACKNOWLEDGED re-drive resolves to synced.** It advances the intent `pending_sync -> synced`,
  persists the platform's external mapping, and sets `sync_reconcile_state='resolved'` — exactly the
  DCA-155 acknowledged outcome, reached under the same key (so a lost ack whose write had in fact
  succeeded resolves to the SAME record).
- **An unconfirmed re-drive is marked for operator review; it NEVER implies success.** A still-PENDING /
  unavailable-lookup outcome leaves the intent `pending_sync` (no accepted status, no external ids) and
  sets `sync_reconcile_state='needs_review'`. `needs_review` is **not terminal** — a later reconcile
  still resolves it once the host acknowledges.

Idempotent and fail-closed: an already-`synced` intent short-circuits (no re-drive, no duplicate, no
second audit) and returns its existing refs; authority is checked first; an unknown / cross-tenant id is
not-found; a non-uncertain intent is refused; the request is re-gated immediately before the re-drive so
a restricted CONTACT field / sendable sync / disallowed purpose is refused with no port call and no state
change; production refuses the contract outright. Reconciling performs **no send**, confers **no
recipient consent** (acceptance is not consent) and creates **no audience membership**. It stages into
the caller's session and never commits (the route/worker owns the unit of work), so the status advance
and its atomic DCA-026 audit entry commit or roll back together.

## Acceptance criteria → evidence

| Criterion | Evidence |
|---|---|
| Retries cannot create duplicate audience members | Re-drive under the stable request key → deterministic external ids (`test_redrive_is_duplicate_safe_under_the_same_request_key`); already-synced is a no-op with no re-drive/audit (`test_idempotent_already_synced_is_a_noop`) |
| Unsupported status lookup does not imply success | An unconfirmed (still-pending) re-drive marks `needs_review`, keeps `pending_sync`, persists no external ids (`test_unconfirmed_redrive_marks_operator_review`) |
| Resolve ambiguous outcomes or mark operator review | ACKNOWLEDGED → synced (`test_acknowledged_redrive_resolves_to_synced`); unconfirmed → `needs_review`, non-terminal (`test_needs_review_is_not_terminal_and_resolves_on_a_later_ack`) |
| Reproduce the relevant denial without unintended state changes | Not-uncertain, restricted-field, marketing-purpose, production, unauthorized viewer, cross-tenant — each refused with no state change / no port call (tests in `tests/test_acceptance_sync_reconcile.py`) |

## Changed files

- `app/db/models.py` — `AcceptanceIntent` gains `sync_reconcile_state` / `reconcile_attempt_count` /
  `last_reconciled_at` (DCA-156 reconciliation dimension).
- `migrations/versions/0070_acceptance_sync_reconcile.py` — add-column branch (guard style of 0069); no
  RLS change (the table is already tenant-owned / RLS-protected).
- `app/services/acceptance_sync_reconcile.py` — the `UncertainAudienceSyncReconciler` service.
- `tests/test_acceptance_sync_reconcile.py` — service unit tests + a PG-gated tenant-scoped/atomic test.
- `tests/test_migrations.py` — `test_0070_adds_sync_reconcile_columns_on_a_preexisting_database`.
- `scripts/manual_check.py` — `@register("DCA-156")` inspectable scenario.

No HTTP route was added (DCA-155 is likewise service-only; a Web/Admin consumer is a later story). No AI
capability, no `.NET`/Web/Admin change. The live audience-sync adapter stays disabled (DCA-001 B1).

## Manual test

```text
python scripts/manual_check.py DCA-156
```

Prints the real state of each case and a `checks: {...}` line with every value `True`: acknowledged
re-drive resolves to synced with the same deterministic external record; replay is an idempotent no-op;
an unconfirmed re-drive marks `needs_review` without implying success; a restricted contact field is
refused and never promoted; a never-synced intent is not uncertain; production refuses the contract; a
cross-tenant id is not-found. Local synthetic only; no AI/paid action, no network, no CRM write, no send.

PG-gated tests (`tests/test_acceptance_sync_reconcile.py`, `tests/test_migrations.py`) run only when
`DCA_TEST_DATABASE_URL` points at a disposable PostgreSQL; they skip otherwise.
