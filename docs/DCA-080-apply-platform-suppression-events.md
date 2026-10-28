# DCA-080 — Apply platform suppression events

Stage: Consent and restriction authority · key `suppression-sync` · source sections 5, 10, 11.

## Prerequisite note

DCA-079 (`define authoritative suppression lookup`) is present (`app/suppression/contract.py` +
`app/suppression/fake.py`) and the earlier sequence is committed, including the two guards this story
composes: the DCA-028 inbox sink (`app/inbox/sink.py`) and the DCA-031 revision projector
(`app/projection/revision.py`), plus the DCA-029 envelope (`app/integration/envelope.py`) and DCA-030
webhook verification (`app/integration/webhook.py`). This story reuses those tables and writers, so it
touches **no DB schema** — no migration is needed.

## What this adds

The **push** side of host-owned suppression. DCA-079 is the pull side (the DCA asks "is this contact
point suppressed right now?"); DCA-080 consumes the host's authoritative suppression **events** into a
**tenant-scoped local projection** so the DCA's view can be kept current between lookups. Suppression
and recipient consent stay the host's to own — this only mirrors the host's decisions, it never
originates one, authorizes no send, and is **not** recipient consent.

- `app/suppression/sync.py`
  - `PlatformSuppressionProjector` — the single sanctioned consumer. Bind it to a verified
    `TenantContext` via `for_context` (scope = the context's business). `apply(envelope)` consumes one
    **verified** `IntegrationEventEnvelope` and returns a `SuppressionSyncOutcome`. Like the
    DCA-026/027/028/031 writers it stages into the **caller's** session and performs no I/O; the caller
    commits the revision change and the inbox receipt together.
  - `SuppressionState` — `blocked` | `cleared` (the host's decision for a `(contact_ref, channel)`).
  - `SuppressionSyncDecision` — `applied` | `stale` | `gap` | `duplicate` | `denied`, plus stable
    reason codes (`scope_mismatch`, `authority_not_platform`, `unrecognized_event_type`,
    `malformed_suppression_payload`, …).
  - `current(contact_ref, channel)` / `is_blocked(...)` — read the projected state (tenant-scoped).

### "Verified" event

A suppression event is consumed only after the two gates already built run **upstream**:

1. **transport auth** — DCA-030 `verify_webhook` (HMAC signature + timestamp tolerance + key
   rotation); and
2. **envelope validation** — DCA-029 `parse_envelope` (supported version, well-formed, minimized,
   **opaque-only** payload).

`apply` takes the resulting typed envelope. The event payload carries opaque handles only — a
`contact_ref` is a host-supplied hashed/tokenized reference, **never a raw contact point** (the
envelope and the projection/inbox screens would refuse an email/phone). This consumer is wired into
**no live route** yet (the inbound transport remains a disabled future capability).

### The event shape

- `event_type = "platform.suppression.updated"` (any other type → DENIED `unrecognized_event_type`).
- `aggregate_version` — the host's monotonic version of this `(contact_ref, channel)` suppression
  entry (drives the revision guard).
- `payload = { authority_plane, state, channel, contact_ref, reason }`.

Each suppressed `(contact_ref, channel)` is one DCA-031 aggregate
(`aggregate_type = "platform-suppression"`, `aggregate_id = "<contact_ref>\x1f<channel>"`) with its own
revision line.

## The two guards (acceptance)

- **A stale event cannot clear a newer block.** The DCA-031 revision guard rejects any event whose
  `aggregate_version` is `<=` the applied version as **STALE** and mutates nothing — so a delayed
  `cleared` at revision 1 can never undo a `blocked` already applied at revision 2. An out-of-order
  successor (`> applied + 1`) is buffered as a visible **GAP**, never applied over the gap.
- **A tenant event cannot widen its own authority.** Authority is checked **first**, fail-closed,
  before any state is touched:
  - the event's `business_id` must be the consuming tenant's scope (taken from the verified context,
    never the event) — else DENIED `scope_mismatch`; and
  - the event must assert **platform** authority (`authority_plane == "platform"`) — suppression is
    host-owned, so a tenant-plane event is a tenant asserting an authority it does not hold → DENIED
    `authority_not_platform`.
  A denial stages **no row** (verified in-session: `session.new == []`).
- **Inbox guard.** The event's `event_id` is recorded as a processed `InboxReceipt`
  (`event_source = "effortlesspromo-platform-suppression"`), so an exact re-delivery is a **DUPLICATE**
  no-op and a failed unit of work rolls the receipt back for a safe retry.

## Boundary

Python DCA only. No DB schema change / no migration (reuses `revision_projections`,
`revision_pending_events`, `inbox_receipts`). No AI, no send, no live route, no cross-service DB write.
Future capabilities (the live inbound transport, acting on a suppression) stay disabled.

## Tests

`tests/test_suppression_sync.py`:

- **Always-on (in-memory SQLite):** first event blocks; contiguous block→clear; **stale clear cannot
  clear a newer block** (headline); exact re-delivery de-duplicated by the inbox guard; out-of-order
  successor is a visible gap; **cross-tenant** event DENIED with no staging; **tenant-plane** event
  DENIED (and cannot clear an existing platform block); unrecognized type / malformed payload DENIED;
  tenant isolation; stages-without-committing (revision + receipt atomic); scope required.
- **Disposable PostgreSQL (`DCA_TEST_DATABASE_URL`):** stale-cannot-clear-a-newer-block on real
  Postgres/transactions; a denied cross-tenant event persists nothing.

Run: `pytest tests/test_suppression_sync.py`. Quality gate: `ruff check .`, `ruff format --check .`,
`mypy app/suppression/sync.py`, `pytest`.

## Developer manual test

```
python scripts/manual_check.py DCA-080
```

Delivers duplicate, delayed (stale) and incorrectly-scoped fixture events for synthetic Businesses
A/B and prints the local restriction + persisted projection/inbox rows after each step (local
synthetic only; no AI/paid action). Expected: block applied; stale clear → still `blocked`; replay →
duplicate no-op; cross-tenant → DENIED `scope_mismatch`, unchanged; tenant-plane → DENIED
`authority_not_platform`, unchanged; no projection for B.
