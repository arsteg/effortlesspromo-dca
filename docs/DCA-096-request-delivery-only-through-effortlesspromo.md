# DCA-096 — Request delivery only through EffortlessPromo

Stage: India channel eligibility · key `delivery-port` · source sections 5, 10.

## What this adds

A typed, DCA-side **delivery adapter contract** (`app/delivery/`). The DCA never sends anything itself
and holds no marketing-provider SDK, key, or send path. The only shape it defines is a *request-
delivery* port: the DCA hands the EffortlessPromo platform a fully-decided dispatch (the DCA-095 grant
it already consumed) under an idempotency key, and the platform's **one send pipeline** (ADR-0008) re-
gates and performs the send. There is deliberately **no `send` method, no provider, no channel I/O** —
requesting delivery through EffortlessPromo is all the DCA is ever allowed to do.

An accepted request is **not** recipient consent and **not** a send authorization — the one send
pipeline owns and performs the actual send, which stays a disabled future capability.

## Design

Mirrors the DCA-022 entitlement and DCA-079 suppression contracts: typed shapes + a client `Protocol`
+ a deterministic fail-closed decision, a synthetic fake alongside, and a live adapter deferred.

- **`DeliveryRequest`** — the contract's typed input: an `idempotency_key` (the SAME consume-once key
  the DCA-095 recheck used, so a retried request is the SAME logical delivery, never a second send) and
  a **`DeliveryContext`** (the decision context the dispatch rested on — the consumed slot, the
  recipient as a one-way `recipient_digest` (never the raw address), the channel, and the
  permission/source/suppression versions). There is no message body: the DCA does not compose or hold
  content; the pipeline owns rendering and the send.
- **`DeliveryPort`** — one method, `request_delivery(request) -> DeliveryReceipt`, and deliberately no
  send/provider/channel-I/O method.
- **`DeliveryCapabilities`** — what the platform delivery contract advertises for a business:
  `availability`, `version`, `as_of` (freshness), `supported_channels`, `host_owns_send=True`, and the
  load-bearing **`callback_enforcement`** — whether the platform GUARANTEES it will report every
  terminal outcome (delivered / bounced / complaint) back to the DCA.
- **`authorize_delivery(...)`** — a pure, fail-closed decision over capabilities: absent/unavailable →
  stale → **callback-enforcement-missing** → channel-not-supported → allowed. It reads nothing and
  changes nothing, so the allow and every denial reproduce exactly.

### Callback enforcement is load-bearing (the acceptance behaviour)

Requesting delivery blinds the DCA unless the platform guarantees terminal-outcome callbacks — those
callbacks are what keep suppression and consent current. A host that does **not** enforce delivery
callbacks therefore **DISABLES live DCA delivery** (`callback_enforcement_unavailable`); the DCA must
never fall back to a fire-and-forget request, which is the exact fail-open this story forbids. An
unavailable or stale host fails closed the same way. The synthetic fake returns a DISABLED receipt
(nothing accepted, empty `host_reference`) in each of those cases — never a silent accept.

## Acceptance evidence

- **Missing callback-enforcement disables live delivery** — a request through a non-callback-enforcing
  host is `DISABLED` with reason `callback_enforcement_unavailable` and accepts nothing; it is never
  bypassed (manual check step 3, `tests/test_delivery_contract.py`).
- **Reproduced with no state change** — the contract is pure (no DB, no network, no send); the allow,
  the disable, and the denials are deterministic and leave nothing behind. A callback-enforcing host
  ACCEPTS the request into the pipeline (idempotent in its key) but the receipt never carries
  `authorizes_send`; the port exposes only `request_delivery` (no provider/send path).

## Run it

```text
pytest tests/test_delivery_contract.py
python scripts/manual_check.py DCA-096
```

The contract has no datastore, locking, or RLS of its own, so no PostgreSQL is needed for this story
(the DCA-095 consume-once ledger it rides on was accepted separately).

## Boundary

DCA-only (Python). No AI-service, Web, Admin, or .NET change. No new table, migration, or HTTP route —
a pure contract package (`app/delivery/`) + its fake, tests, manual-check scenario, and this doc. The
live HTTP adapter into the platform's one send pipeline is a later story (blocked on DCA-001 **B1** —
there is no service-to-service contract into the .NET API yet); production refuses the fake, so the
contract stays disabled until that adapter ships. Acting on an accepted request (an actual send) stays
a disabled future capability behind the one send pipeline.
