# DCA-097 — Request cancellation of queued delivery

Stage: India channel eligibility · key `cancel-queued` · source sections 5, 10.

## What this adds

A typed, DCA-side **cancellation adapter contract** (`app/delivery/cancellation.py` + its fake). It is
the mirror image of the DCA-096 delivery port: where DCA-096 lets the DCA REQUEST delivery of a decided
dispatch through the EffortlessPromo one send pipeline (ADR-0008), DCA-097 lets the DCA REQUEST
cancellation of that queued work when a permission it relied on is later **invalidated** (consent
withdrawn, the contact newly suppressed, a source right expired) after the dispatch was already handed
to the host.

The DCA never sends and never recalls — it holds no marketing-provider SDK, key, send path, or recall
path. The only shape it defines is a *request-cancellation* port: one method, `request_cancellation`,
and deliberately **no `recall` / `unsend` / `send` / provider / channel-I/O method**. The platform owns
the queue and performs (or refuses) the cancellation; requesting it through EffortlessPromo is all the
DCA is ever allowed to do.

## Design

Mirrors the DCA-096 delivery contract: typed shapes + a client `Protocol` + a deterministic fail-closed
decision, a synthetic fake alongside, and a live adapter deferred.

- **`QueuedWorkState`** — the host-side state of one piece of queued work: `QUEUED` (still inside the
  pipeline, cancelable) or `PROVIDER_ACCEPTED` (already accepted by a provider — non-recallable).
- **`QueuedWorkRef`** — a pointer to one queued dispatch to stop: the SAME DCA-095/096 consume-once
  `idempotency_key`, the channel, the recipient as a one-way `recipient_digest` (never the raw
  address), and the `consumption_id`. No message body — the DCA names a dispatch, it does not hold
  content.
- **`CancellationRequest`** — a `cancellation_key` (consume-once idempotency for the cancellation op
  itself), the `invalidation_reason` (WHY the permission is no longer valid — audit provenance), the
  `items` to cancel, and a `correlation_id`.
- **`CancellationCapabilities`** — what the platform cancellation contract advertises: `availability`,
  `version`, `as_of` (freshness), `supports_cancellation`, `supported_channels`, `host_owns_send=True`,
  and the load-bearing **`acknowledges`** — whether the platform GUARANTEES it will acknowledge every
  cancellation request.
- **`CancellationReceipt`** — `acknowledged`, a per-item `outcomes` tuple, `canceled_count` /
  `non_recallable_count`, and a deterministic `acknowledgment_reference` (idempotent in the
  cancellation key). `authorizes_send` is always `False`.
- **`CancellationPort`** — one method, `request_cancellation(request) -> CancellationReceipt`.
- **`authorize_cancellation(...)`** — a pure, fail-closed decision over capabilities: absent/unavailable
  → stale → cancellation-not-supported → acknowledgment-not-guaranteed → allowed. It reads nothing and
  changes nothing, so the allow and every denial reproduce exactly.

### The two load-bearing behaviours (the acceptance)

1. **Not everything can be stopped — label, don't pretend.** When the DCA requests cancellation of a
   set of queued-work items, each is resolved against its host-side state: a still-`QUEUED` item is
   actually `CANCELED` (removed before send); an already-`PROVIDER_ACCEPTED` item is **labeled
   `NON_RECALLABLE`** and left in place (the honest outcome — the DCA never pretends an in-flight send
   was stopped); an unknown key is `NOT_FOUND`. "Remaining queued work is canceled; already-provider-
   accepted messages are labeled non-recallable where applicable."

2. **Acknowledgment is load-bearing and fail-closed.** An unacknowledged "cancel" is indistinguishable
   from a dropped one, so the DCA could never know the queued send was actually stopped. A host that
   does **not** guarantee acknowledgment — or does not support cancellation, or is unavailable/stale —
   therefore **DISABLES** the request (`cancellation_acknowledgment_unavailable` /
   `cancellation_not_supported` / `cancellation_host_unavailable` / `cancellation_capabilities_stale`)
   and leaves the host queue **UNTOUCHED**, never treating an unconfirmable cancel as a success.

The fake keeps an in-memory acknowledgment ledger keyed on the `cancellation_key`, so a **replayed**
cancellation returns the SAME receipt verbatim (acknowledgment tracking, visible) and never re-reads a
now-mutated queue into a different answer. A **disabled** request is deliberately NOT ledgered, so once
the host recovers the same key may retry and succeed.

## Acceptance evidence

- **Queued work canceled, provider-accepted work labeled non-recallable** — an acknowledging host over
  a seeded queue (one `QUEUED`, one `PROVIDER_ACCEPTED`) cancels the queued item (it is gone from the
  queue), labels the provider-accepted item `NON_RECALLABLE` (left in place), and reports an unknown key
  as `NOT_FOUND` (manual-check step 1, `tests/test_cancellation_contract.py`).
- **Reproduced with no state change on denial** — a non-acknowledging / unsupported / unavailable /
  stale host DISABLES the request, returns no outcomes, and leaves the queue exactly as it was
  (manual-check step 3). The decision function is pure (no DB, no network, no send, no recall); the
  allow, the disable, and every denial are deterministic. The port exposes only `request_cancellation`
  (no recall/send/provider path), and a receipt never carries `authorizes_send`.

## Run it

```text
pytest tests/test_cancellation_contract.py
python scripts/manual_check.py DCA-097
```

The contract has no datastore, locking, or RLS of its own, so no PostgreSQL is needed for this story —
exactly as the DCA-096 delivery contract it mirrors (the DCA-095 consume-once ledger they ride on was
accepted separately). The acknowledgment tracking is in-memory in the fake, because there is no live
host to acknowledge a real cancellation yet (see Boundary).

## Boundary

DCA-only (Python). No AI-service, Web, Admin, or .NET change. No new table, migration, or HTTP route —
a pure contract package (`app/delivery/cancellation.py` + `app/delivery/cancellation_fake.py`), its
fake, tests, a manual-check scenario, and this doc. The live HTTP adapter into the platform's one send
pipeline is a later story (blocked on DCA-001 **B1** — there is no service-to-service contract into the
.NET API yet); production refuses the fake (`build_cancellation_port`), so the contract stays disabled
until that adapter ships. Acting on a cancellation (actually stopping a real queued send) stays a
disabled future capability owned by the one send pipeline.
