# DCA-079 — Define authoritative suppression lookup

Stage: Consent and restriction authority · key `platform-suppression-port` · source sections 5, 10, 11.

## Prerequisite note

DCA-078 (`store scoped processing restrictions`) is committed at HEAD (`41b961d`), and the earlier
sequence is present. This story is self-contained: a new contract + synthetic fake that depend only on
the DCA-014/022/037 contract-port pattern and the `app.identity.fake` synthetic businesses. It touches
no DB, no RLS, no AI path — so no migration is needed.

## What this adds

The **platform-suppression port**: the contract the DCA uses to consult the host platform's
authoritative suppression registry, plus a deterministic synthetic fake. Suppression and recipient
consent are the **host's** to own; the DCA only ever reads this answer.

- `app/suppression/contract.py`
  - `SuppressionLookupRequest(business_id, correlation_id)` — DCA → platform.
  - `SuppressionSnapshot` — the immutable answer, disclosing the three things the story asks for:
    **version** (`version`), **freshness** (`as_of`), and **affected scope** (`covered_channels` — the
    channels the snapshot authoritatively speaks for — together with `entries`, the suppressed
    `(contact_point, channel)` list). It carries `availability` (`available` | `unavailable`),
    `host_owns_consent=True` (the ownership boundary, stated at the type level), and `synthetic`.
  - `PlatformSuppressionClient` — the `Protocol` port. Only the fake implements it today; the live HTTP
    adapter is a later story and the contract stays **disabled in production** until it ships.
  - `authorize_activation(snapshot, contact_point, channel, *, now, max_age)` → `ActivationDecision`.
    Pure and fail-closed at each step: **absent/unavailable → stale → channel-not-covered →
    suppressed → allowed**. `ActivationDecision.grants_consent` is **always `False`** — an allow only
    removes the suppression blocker and is never recipient consent. Default freshness window is a tight
    **1 hour** (an unsubscribe must be honored fast), deliberately shorter than the 24h entitlement
    window.
- `app/suppression/fake.py`
  - `FakePlatformSuppressionClient` — deterministic, no I/O. `available=False` simulates the registry
    being unreachable (every lookup → `UNAVAILABLE`); `now` is injected into `as_of` so staleness is
    testable without a wall clock. Business A has two suppressed entries
    (`opted-out@synthetic-a.example`/email, `+919900000000`/sms); Business B is clean. Covers
    `email`/`sms`. Every snapshot is `synthetic=True`, `host_owns_consent=True`.
  - `build_platform_suppression_client(settings)` — returns the fake in dev/test; raises
    `SuppressionContractDisabled` under production (never silently hands back a fake, which would risk
    treating a synthetic "clear" as authoritative — the exact fail-open this story forbids).
- `app/suppression/__init__.py` — package exports.

## Acceptance criteria → evidence

- **Unavailable/stale authoritative responses produce no activation permission.** `authorize_activation`
  returns `LOOKUP_UNAVAILABLE` for an unavailable (or absent) snapshot and `STALE` past the freshness
  window — in both cases `allowed is False`, even for a contact point with no suppression entry
  (absence of a record is not evidence of its absence). Covered by
  `test_unavailable_lookup_produces_no_activation_permission`,
  `test_absent_snapshot_produces_no_activation_permission`,
  `test_stale_snapshot_denies_even_when_not_suppressed`, `test_freshness_boundary_respects_max_age`,
  `test_fail_closed_order_unavailability_beats_everything`.

- **Reproduce the result and the relevant denial without unintended state changes or later-story
  work.** The gate is a pure function of the snapshot and the moment — it reads nothing and persists
  nothing, so the allow (`test_available_fresh_and_not_suppressed_permits_activation`) and every denial
  (suppressed / unavailable / stale / channel-not-covered) are perfectly reproducible with no state to
  change. An allow is not consent (`test_an_allow_is_not_recipient_consent`). Nothing is activated or
  sent — acting on an activation stays a disabled future capability behind the one send pipeline.

## Files

- `app/suppression/__init__.py`, `app/suppression/contract.py`, `app/suppression/fake.py` — the port +
  fake.
- `tests/test_platform_suppression.py` — behaviour + every failure case.
- `scripts/manual_check.py` — the `DCA-079` inspectable scenario.
- `docs/DCA-079-define-authoritative-suppression-lookup.md` — this note.

No migration: the port holds no state and touches no DB.

## Manual check

```
python scripts/manual_check.py DCA-079
```

Queries active / stale / unavailable fake suppression states for synthetic Businesses A/B in an
ephemeral SQLite DB, printing each gate input and result: active+fresh clears a non-suppressed contact
point (ALLOW) and denies a suppressed one; a stale answer and an unavailable answer each DENY with no
activation permission; a channel outside the authoritative scope is refused; production refuses the
fake. Local synthetic only; AI disabled; nothing is persisted and no send/consent is granted.
