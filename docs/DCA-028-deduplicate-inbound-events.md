# DCA-028 — Deduplicate inbound events

**Status:** implemented. Adds a DCA **inbox** (idempotent receiver) — a tenant-scoped receipt store
keyed by an inbound event's external `(event_source, event_id)`, staged **inside the caller's
transaction** so handler completion is transactional. It persists the receipt only; it performs **no
handler orchestration, no relay, no remote HTTP calls** (those stay disabled). No other service
changed; no UI; no AI-service internals. This is the inbound counterpart of the DCA-027 outbox.

## What it does

`InboxSink` (`app/inbox/sink.py`) is the single sanctioned writer of the new `inbox_receipts` table
(`app/db/models.py`). `sink.register(...)` only `session.add(...)`s a receipt — it never commits,
flushes, or opens a transaction of its own, and it does no I/O. The caller commits the handler's local
change and its receipt together, so the two share one unit of work. This mirrors DCA-026's audit sink
and DCA-027's outbox sink (the platform's atomic-with-the-caller rule, SEC-001 / BR-122).

Columns (opaque only — never personal data):

| Column | Meaning |
|---|---|
| `business_id` | the tenant scope (from `TenantOwned`; stamped from the verified context, never a caller argument) |
| `event_source` | controlled source code of the sending system, e.g. `effortlesspromo-api` — first half of the dedup key |
| `event_id` | the sending system's **own** event id — second half of the dedup key (not re-derived; it is the external natural key) |
| `event_type` | optional controlled event-type code, e.g. `candidate.enriched` |
| `payload` | opaque references (ids / row keys) — JSON, screened free of secrets/contact data |
| `status` | `pending` → `processed`; receipts are created `processed` (completion is transactional). `pending` is reserved for a future, disabled claim/lease capability |
| `received_at`, `processed_at` | staged timestamps; both set at receive time here |

### A replay cannot apply the local change twice

`(business_id, event_source, event_id)` is unique (`tenant_unique("event_source", "event_id",
name="uq_inbox_event")`). Replaying the same event re-stages the same receipt; the constraint rejects
it **at commit** (an `IntegrityError`) and the caller's rollback takes the handler's partial change with
it — the local effect is applied **exactly once**. `InboxSink.already_processed(...)` is a tenant-scoped
read fast-path so a caller can short-circuit a known replay and do no handler work at all, instead of
working then rolling back. The constraint is *per business*, so two tenants may receive the same
source + id without colliding.

### Failed processing can retry safely

Because the receipt and the handler's change share one transaction, a handler failure rolls back
**both** — no receipt and no partial change are left behind, so the retry sees a clean slate and applies
the event once.

### No raw secrets or contact data

Every `event_source` / `event_id` / `event_type` and every `payload` value is screened **before**
anything is staged, reusing the single DCA-012 redaction policy via the audit screens
(`assert_opaque_text`, `assert_opaque_references`). A value carrying an email, a phone number, a
registered secret, or a sensitive key is refused with `InboxPayloadError` and no row is created.

### Tenant isolation

`inbox_receipts` is a `TenantOwned` table, so it is automatically in `tenant_owned_tables()` and is
covered by the DCA-018 row-level-security policy exactly like the other tenant data tables — one tenant
can never read another's receipts. Migration `0007_inbox_receipts` adds the table + RLS policy for
databases that predate it (on a fresh DB the metadata baseline + 0002 already cover it; 0007 is a
guarded no-op there — same dual-path style as 0005 / 0006).

## Tests

```
# unit + replay-denial (always, in-memory SQLite):
pytest tests/test_inbox_sink.py

# transactional handler completion + the 0007 create branch + RLS coverage (disposable Postgres):
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55474:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55474/dca"
pytest tests/test_inbox_sink.py tests/test_migrations.py tests/test_rls.py
```

Key cases: `test_replay_does_not_apply_the_local_change_twice`,
`test_replay_without_fast_path_is_rejected_at_commit`, `test_failed_processing_can_retry_safely`
(Postgres), plus the always-on contract, replay-denial and redaction-denial unit tests. Quality gate
(all clean): `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```
python scripts/manual_check.py DCA-028
```

Seeds two synthetic candidates for Business A, then: (1) delivers event `E1` → handled; (2) delivers
`E1` again → fast-path no-op (no second application), and (2b) re-registers `E1` without the fast path
→ rejected by the uniqueness constraint; (3) delivers `E2` but fails mid-handler → both the change and
the receipt roll back, so (4) the retry applies it cleanly; (5) refuses a receipt carrying a raw email.
It prints the actual candidate statuses and the persisted `inbox_receipts` rows — expect both
candidates `accepted` (each applied exactly once), two `processed` receipts, the duplicate rejected, the
failed delivery rolled back and retried, and the raw-contact payload refused. Local synthetic SQLite by
default (also runnable against a disposable Postgres via `--database-url`); no AI/paid action; the
ephemeral DB is cleaned up.

## Boundary

Inside the DCA only: new model + `uq_inbox_event` constraint, migration `0007_inbox_receipts`,
`app/inbox/`, focused tests, the two tenant-owned-table assertions updated (`tests/test_rls.py`,
`tests/test_migrations.py`), this doc, and the DCA-028 manual-check scenario. The inbox is **not** wired
into any HTTP route and has **no handler-orchestration or relay path** — that is a separate, disabled
story. It reuses existing screens (`app/audit/`) with no change to them. It is a ready, independently
reviewable persistence capability. Future capabilities stay disabled.
