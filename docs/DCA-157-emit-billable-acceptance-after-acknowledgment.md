# DCA-157 — Emit billable acceptance after acknowledgment

Stage: User selection and EffortlessPromo sync · Key: `acceptance-meter` · Status: **Ready for manual review**

## What this adds

After the DCA-155 sync / DCA-156 reconcile drives a DCA-153 `AcceptanceIntent` to `synced` (its external
mapping persisted by an **acknowledged** platform acknowledgment), the accepted prospect becomes
**billable**. DCA-157 records that durable, idempotent fact with a single tenant-scoped service,
`BillableAcceptanceEmitter` (`app/services/billable_acceptance.py`), mirroring the DCA-153 recorder and
the DCA-155/156 services.

It records a `BillableAcceptance` ledger entry **and** stages a durable DCA-027 outbox **usage record**,
both in the caller's unit of work. It performs **NO live billing API call and charges nothing** — the
actual delivery of usage to platform billing is the later, currently-**disabled** story (DCA-158). The
outbox record stays `pending` (never delivered here), exactly like the DCA-153 pending-sync request.

The load-bearing invariant: **a prospect is billable exactly once, only on acknowledgment — never on a
pending / uncertain / failed sync, and never double-charged on a replay / reconcile.**

- **Only an ACKNOWLEDGED sync is billable.** Emitted only when the intent is `status='synced'`,
  `sync_status='acknowledged'`, and the external mapping is present. A `pending_sync` (never-synced), a
  `pending` (lost-acknowledgment) hand-off, or a `needs_review` (unconfirmed reconcile) intent is
  **refused** (`acceptance_sync_not_acknowledged`) with no ledger entry, no usage record and no audit.
- **Billable once; never double-charged.** `(business_id, acceptance_intent_id)` is unique and the
  `billing_key` is the **stable logical billing key** (`billable_acceptance_key`, a uuid5 deterministic
  in business + intent). A duplicate acknowledgment — a replay, or a DCA-156 reconcile that re-resolved
  the SAME intent under the SAME request key — short-circuits on the existing entry (`already_emitted`):
  no second entry, no second usage record, no second audit.
- **A billable fact is not a send / spend / consent.** Provenance pins the invariants (`billable=True`;
  `delivered` / `sent` / `spent` / `consentConferred` / `membershipCreated` = `False`). `result.charged`
  and `result.confers_consent` are always `False`.

Fail-closed and tenant-scoped: authority (`candidate.accept`, the same scope as accept/sync) is checked
first; an unknown / cross-tenant id reads as not-found; every guard runs before any state is mutated, so
a refusal changes nothing. The service stages into the caller's session and never commits, so the ledger
entry, its DCA-027 usage record and its atomic DCA-026 audit entry commit (or roll back) together.

## Acceptance criteria → evidence

| Criterion | Evidence |
|---|---|
| No charge event for excluded / failed / pending records | `test_not_emitted_for_never_synced_intent`, `test_not_emitted_for_pending_uncertain_sync`, `test_not_emitted_for_needs_review_intent` — each refused `acceptance_sync_not_acknowledged`, 0 entries / usage / audits |
| Duplicate acknowledgment emits one event | `test_replay_is_idempotent_no_double_charge`, `test_reconcile_re_resolution_never_double_charges` — one entry, one usage record, one audit |
| Stable logical billing key | `test_emits_billable_on_acknowledgment` asserts `billing_key == billable_acceptance_key(biz, intent)` |
| Emitted only after acknowledgment, with the external mapping | `test_emits_billable_on_acknowledgment` (status synced + acknowledged precondition, external id captured) |
| No live billing call | ledger + outbox only; `result.charged is False`, provenance `delivered/sent/spent=False` (`test_emits_billable_on_acknowledgment`) |
| Reproduce the relevant denial without unintended state changes | unauthorized viewer + unknown / cross-tenant id refused with no state change (`test_unauthorized_viewer_refused_with_no_state_change`, `test_unknown_or_cross_tenant_id_is_not_found`) |

## Changed files

- `app/db/models.py` — `billable_acceptance_key()` + `_BILLABLE_ACCEPTANCE_NS`, `BillableAcceptanceStatus`
  enum, and the tenant-owned `BillableAcceptance` ledger table (`uq_billable_acceptance_intent`).
- `migrations/versions/0071_billable_acceptance.py` — create-branch for `billable_acceptances` + its RLS
  policy/grant (guard style of 0068); no-op on a fresh DB (baseline 0001 + RLS 0002 already build it).
- `app/services/billable_acceptance.py` — the `BillableAcceptanceEmitter` service.
- `tests/test_billable_acceptance.py` — service unit tests + a PG-gated tenant-scoped/atomic test.
- `tests/test_rls.py` — `billable_acceptances` added to the covered-tables set + isolation / write-check
  tests.
- `tests/test_migrations.py` — `billable_acceptances` added to `DCA_TABLES` +
  `test_0071_creates_billable_acceptances_on_a_preexisting_database`.
- `scripts/manual_check.py` — `@register("DCA-157")` inspectable scenario.
- `docs/DCA-157-emit-billable-acceptance-after-acknowledgment.md` — this doc.

No HTTP route was added (DCA-155/156 are likewise service-only; a Web/Admin consumer is a later story).
No AI capability, no `.NET`/Web/Admin change. **Live billing delivery stays disabled (DCA-158).**

## Manual test

```text
python scripts/manual_check.py DCA-157
```

Prints the real state of each case and a `checks: {...}` line with every value `True`: an acknowledged
intent emits one billable entry (+ one usage record + one atomic audit) under the stable billing key with
no live billing call / charge; a replay and a DCA-156 reconcile re-resolution never double-charge; a
pending / uncertain / needs_review intent emits nothing; an unauthorized viewer and a cross-tenant id are
refused with no state change. Local synthetic only; no AI/paid action, no network, no send, no charge.

PG-gated tests (`tests/test_billable_acceptance.py`, `tests/test_rls.py`, `tests/test_migrations.py`) run
only when `DCA_TEST_DATABASE_URL` points at a disposable PostgreSQL; they skip otherwise.
