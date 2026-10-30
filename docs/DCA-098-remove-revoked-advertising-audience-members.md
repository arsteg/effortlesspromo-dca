# DCA-098 — Remove revoked advertising audience members

Stage: India channel eligibility · key `audience-remove` · source sections 5, 10.

## What this adds

A typed, DCA-side **advertising-audience removal contract** (`app/advertising/audience_removal.py` + its
fake). DCA-091 decided whether a customer-list audience *may be uploaded* to an advertising platform
(Google Customer Match). DCA-098 is the mirror on the way out: when a member whose identifier was
previously uploaded has their permission **withdrawn** (consent revoked, newly suppressed, a source right
expired), the DCA must get that member **removed** from the audience so the ad platform stops targeting
them.

Like delivery (DCA-096) and cancellation (DCA-097), the DCA never touches an ad-platform SDK, key, or
audience API — it can only ever *REQUEST* a removal through EffortlessPromo (the integration the platform
owns). The only shape defined is a *request-removal* port: one method, `request_removal`, and deliberately
**no `upload` / `add` / `sync` / `send` / provider method**. A removal is expressed as the platform's
audience-**update "remove"** operation. The DCA names a member to remove (by one-way hashed digest, never
the raw identifier); it can never add a member or perform the integration I/O itself.

## Design

Mirrors the DCA-097 cancellation contract: typed shapes + a client `Protocol` + a deterministic
fail-closed decision, a synthetic fake alongside, and a live adapter deferred.

- **`AudienceMemberState`** — the host-side state of one previously-uploaded member: `PRESENT`
  (removable) or `REMOVED` (already gone — a removal is a no-op reported `NOT_PRESENT`).
- **`AudienceMemberRef`** — a pointer to one member to remove: the one-way `member_digest` (the SHA-256
  hashed identifier exactly as DCA-091 uploads it, never the raw address), the `key_type`, and an
  optional `source_membership_id`. No raw PII and no content (`extra="forbid"` keeps a raw address from
  sneaking in).
- **`RemovalRequest`** — a `removal_key` (consume-once idempotency for the removal op itself), the
  `withdrawal_reason` (WHY the permission is no longer valid — audit provenance), the `audience_channel`
  (e.g. `google_customer_match`), the `members` to remove, and a `correlation_id`.
- **`RemovalCapabilities`** — what the platform removal contract advertises: `availability`, `version`,
  `as_of` (freshness), `supports_removal`, `supported_audiences`, `host_owns_audience=True`, and the
  load-bearing **`acknowledges`** — whether the platform GUARANTEES it will acknowledge every removal.
- **`RemovalReceipt`** — `status` (`COMPLETED` / `PENDING`), `acknowledged`, a per-member `outcomes`
  tuple, `removed_count` / `not_present_count` / `pending_count` / `has_pending`, an
  `acknowledgment_reference` (deterministic in the removal key, only on a COMPLETED request), and the
  always-`True` **`withdrawal_upheld`**. `authorizes_send` is always `False`.
- **`RemovalPort`** — one method, `request_removal(request) -> RemovalReceipt`.
- **`authorize_removal(...)`** — a pure, fail-closed decision over capabilities: absent/unavailable →
  stale → removal-not-supported → acknowledgment-not-guaranteed → audience-not-supported → allowed. It
  reads nothing and changes nothing, so the allow and every denial reproduce exactly.

### The two load-bearing behaviours (the acceptance)

1. **The withdrawal is authoritative — the prior upload never overrides it.** The moment a member is
   named in a removal request the DCA's own truth is *withdrawn*, independent of the host outcome. A
   member still `PRESENT` on the host because its removal is not yet confirmed is NOT treated as active
   again: `RemovalReceipt.withdrawal_upheld` is always `True`, and the fake's `membership(digest)` reports
   such a member as `withdrawn` even while the host still shows it `present`. The stale fact that the
   member was once uploaded can never flip the current withdrawal back to membership.

2. **Removal failures stay visible and pending — never silently dropped, never faked as done.** A member
   the host could not remove right now (a per-member temporary platform failure), or an entire request to
   a host that is unavailable / stale / non-acknowledging / audience-blind, is labeled
   `RemovalItemStatus.PENDING`, kept *owed*, and left in the audience (`UNTOUCHED` on a host-level
   failure) — the request `status` is `PENDING` and it is NOT ledgered, so the DCA retries the same key
   until the host confirms the removal. An unconfirmable removal is never reported as `REMOVED`. Only a
   fully-confirmed request is `COMPLETED` and idempotently ledgered, so a replayed completed removal
   returns the SAME receipt verbatim (and never re-reads a now-removed member into a different
   `NOT_PRESENT` answer).

The fake keeps three in-memory sets: `_withdrawn` (every member ever named — the authoritative withdrawal),
`_owed` (removals still pending, kept visible), and a `_ledger` of fully-COMPLETED receipts keyed on the
`removal_key`. A PENDING request is deliberately NOT ledgered, so once the host recovers (`recover(...)`
clears a transient failure; flipping `available`/`acknowledges` recovers a host-level failure) the same
key retries and succeeds.

## Acceptance evidence

- **Present member removed, absent member not-present, failures pending** — an acknowledging host over a
  seeded audience (one `PRESENT`, one `REMOVED`) removes the present member, reports the already-gone and
  never-uploaded members `NOT_PRESENT`, and a per-member transient failure or an unavailable host leaves
  the removal `PENDING` and owed with the audience untouched (manual-check steps 1–4,
  `tests/test_audience_removal_contract.py`).
- **The prior upload never overrides the withdrawal** — a member whose removal is still pending (member
  left `PRESENT` on the host) reads as `withdrawn` via the DCA's authoritative view, and
  `withdrawal_upheld` is `True` even on a PENDING/disabled receipt (manual-check step 3,
  `test_removal_failure_stays_visible_and_pending_prior_upload_does_not_override`).
- **Reproduced with no unintended state change on denial** — the decision function is pure (no DB, no
  network, no send, no upload); a host-level failure leaves the audience exactly as it was and is not
  ledgered, so a retry re-attempts and the allow, the pending, and every denial are deterministic. The
  port exposes only `request_removal` (no upload/add/sync/send/provider path), and a receipt never carries
  `authorizes_send`.

## Run it

```text
pytest tests/test_audience_removal_contract.py
python scripts/manual_check.py DCA-098
```

The contract has no datastore, locking, or RLS of its own, so **no PostgreSQL is needed for this story** —
exactly as the DCA-096 delivery and DCA-097 cancellation contracts it mirrors. (The disposable-PostgreSQL
rule in the story applies when a task introduces locking/RLS/transactions; this pure contract introduces
none.) The withdrawal/owed/acknowledgment tracking is in-memory in the fake, because there is no live host
to acknowledge a real removal yet (see Boundary).

## Boundary

DCA-only (Python). No AI-service, Web, Admin, or .NET change. No new table, migration, or HTTP route — a
pure contract package (`app/advertising/audience_removal.py` + `app/advertising/audience_removal_fake.py`
+ `app/advertising/__init__.py`), its fake, tests, a manual-check scenario, and this doc. It reuses
DCA-091's `CUSTOMER_MATCH_CHANNEL` as the audience it speaks for. The live HTTP adapter into the
platform's advertising-audience integration is a later story (blocked on DCA-001 **B1** — there is no
service-to-service contract into the .NET API yet, and no live advertising-audience integration exists);
production refuses the fake (`build_removal_port`), so the contract stays disabled until that adapter
ships. Actually performing a removal against a real ad platform stays a disabled future capability owned
by that integration.
