# DCA-034 — Publish outbox events idempotently

**Status:** implemented. Adds `OutboxPublisher` (`app/outbox/publisher.py`) — the relay that drains
PENDING transactional-outbox messages (DCA-027) and delivers each through the DCA-032 platform HTTP
transport, keyed by the message's **stable** event id so a redelivery after a lost acknowledgment is
the **same** logical event, never a fresh one. It is the relay/consumer DCA-027 deferred. It adds
**no new send path** — it only rides the one hardened, allowlisted platform transport — and is
**config-gated OFF** with no live relay route wired. No provider SDKs/keys, no model routing, no
second AI gateway, no writes to another service's database. Future capabilities stay disabled.

## What it does

`OutboxPublisher.publish_pending(business_id)` reads PENDING `OutboxMessage` rows for one business
(oldest first, batch-capped), and for each POSTs a versioned `IntegrationEventEnvelope` (DCA-029) to
the configured relay path. The message's stable `event_id` (`outbox_event_id(business_id,
event_type, dedup_key)`) is sent as the `Idempotency-Key`.

| Guarantee | How |
|---|---|
| **Stable key** | Each delivery carries the message's deterministic `event_id` as the `Idempotency-Key`. A retry re-sends the identical key, so the platform records the event once however many times a lost ack makes us retry. The publisher never mints a new event on failure. |
| **Acknowledgment tracking** | Only a **2xx** response transitions the row `PENDING → DELIVERED` (stamping `delivered_at`), committed in the publisher's **own** unit of work (never the caller's enqueue transaction). |
| **Lost ack ⇒ retryable, same identity** | A transport **timeout** / **unreachable** host / retryable **5xx**/**429** leaves the row exactly as it was — `PENDING`, `delivered_at` unset, the **same** `event_id`. The next pass redelivers under the same key. |
| **Definitive denial is surfaced, not retried** | A **4xx** (`unauthorized`/`forbidden`/`client_error`) maps to `PublishOutcome.DENIED`: the row still changes no state (stays `PENDING`); a caller sees the denial in the returned `PublishResult`. |
| **Tenant-scoped** | `publish_pending` requires a `business_id` and drains only that business's rows — never a cross-tenant drain. `publish_message(id, business_id)` publishes one row by id, returning `None` for a wrong-tenant/unknown/non-pending row (nothing attempted or changed). |

`publish_*` return one `PublishResult` per attempted message (`outcome`, `idempotency_key`,
`status_code`, `response_class`/`error_kind`) — opaque identifiers + a stable classification only,
never a payload value or credential.

### Off by default (fail-closed)

The publisher rides the platform transport, so it is disabled until **both** the transport is
configured (`platform_base_url` + `platform_service_credential`) **and** a relay path is set
(`platform_outbox_publish_path`) — `Settings.outbox_publisher_configured`. `build_outbox_publisher`
returns `None` when unconfigured (a missing relay contract stays disabled, B1). `validate_required`
rejects a relay path set without the transport, and a non-positive batch size, at boot.

### Config keys (names only)

`platform_outbox_publish_path` (empty ⇒ publisher disabled), `outbox_publish_batch_size` (default
50). The transport keys (`platform_base_url`, `platform_service_credential` + `_secret_ref`, …) are
DCA-032's.

## Boundary

Payloads carry only opaque references — screened at enqueue by the DCA-027 sink and re-screened here
by `new_envelope` (DCA-029). The outbox never holds contact data; **candidate acceptance is not
recipient consent**. No live relay path is wired; in tests and the manual check the publisher is
driven against the DCA-033 fake contract server (`httpx.MockTransport`), so nothing leaves the box.

## Verification

- `tests/test_outbox_publisher.py`:
  - **Always-on (in-memory SQLite)** drives the real platform transport against the DCA-033 fake
    server: 2xx delivers keyed by the stable event id; the versioned envelope is sent; a lost ack
    (timeout) leaves the row PENDING with the same identity, then a retry delivers and **both HTTP
    attempts carry the one Idempotency-Key** (one logical event); unreachable/5xx/429 are retryable;
    4xx is a surfaced denial; scope isolation; batch cap; by-id publish; disabled-without-contract;
    boot rejections.
  - **Disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`) proves the headline criterion over real
    transactions: publish with a lost ack → retry → one logical event, identity preserved. Skips
    without a Postgres URL.
- Manual check: `python scripts/manual_check.py DCA-034` — stages a PENDING event, loses its
  acknowledgment, retries, and shows the fake host recorded one logical event (one stable
  Idempotency-Key across two POSTs), plus the disabled-without-relay-path denial. Local synthetic
  only; no network/AI/paid action.
