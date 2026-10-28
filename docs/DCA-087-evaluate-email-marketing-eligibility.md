# DCA-087 — Evaluate email marketing eligibility

Stage: **India channel eligibility** · key: `email-policy` · source sections 5, 10.

## What this adds

One pure, deterministic decision function — `app/compliance/email_eligibility.py` —
that answers a single question for the **email** channel: *may a marketing email to this
contact point proceed to the host's one send pipeline?* It does this by **composing** the
gates the earlier accepted stories each built in isolation; it introduces no new acquisition,
no send, no AI call, no DB write, and is wired into no pipeline/worker/HTTP path.

### The five combined gates (fail-closed, consent-first)

| # | Gate | Input | Blocks when | Reason code |
|---|------|-------|-------------|-------------|
| 1 | Lawful basis | `PermissionBasis.basis` | a non-consent basis is claimed | `basis_not_consent` |
| 2 | Permission in force | `PermissionBasis.permission_state` (DCA-075) | not `GRANTED` for the email scope | `consent_not_in_force` |
| 3 | Source rights | `SourceRights` (DCA-045/046/047/048) | source may not be used for contact | `source_rights_insufficient` |
| 4 | Freshness | `Freshness.age` vs `max_age` | evidence too old / unknown age | `evidence_stale` |
| 5 | Suppression | `ActivationDecision` (DCA-079/080) | not cleared (unavailable/stale/not-covered/listed) | `suppression_blocked` |
| — | Sender-provider rule | `ProviderRule` (independent) | provider prohibits | `provider_restricted` |

`ELIGIBLE` requires **all** gates to pass. Every failing gate is collected (not
short-circuited) in a fixed order, so the result shows exactly which gates withheld
permission — the provider gate among them, which can deny on its own.

### Load-bearing invariants (acceptance criteria)

- **Consent-first, no invented B2B exception.** The only accepted basis is explicit, in-force
  consent for the `(brand, contact point, email, purpose)` scope. A claimed
  `legitimate_interest` / `b2b` basis is rejected.
- **Verified / high-fit never qualifies an address.** `email_verified` and `fit_band` are
  carried on the decision for audit only (like `SourcePreflight.origin_signals`) and are
  **never** consulted by the allow decision. Absent in-force consent evidence the result is
  `BLOCKED`.
- **Provider restrictions deny independently.** An otherwise-passing address is still
  `BLOCKED` when the provider prohibits.
- **ELIGIBLE is not consent and not a send authorization.** `grants_consent` is always
  `False`; the real send stays a disabled future capability behind the host's one send
  pipeline, which re-gates everything.
- **Pure and reproducible.** The decision reads nothing and writes nothing; a given
  `(request, now)` always yields the identical result.

## Public surface

- `decide_email_eligibility(request, *, now)` — the pure composition over
  `EmailEligibilityRequest` (one value object per gate).
- `evaluate_email_eligibility(...)` — a convenience that derives the permission + freshness
  gates from a DCA-075 `PermissionProjection` and the suppression gate from a DCA-079
  `SuppressionSnapshot` (via `authorize_activation`), leaving source-rights / provider as
  already-resolved inputs. Still pure — no DB, no adapter, no model.
- `basis_from_projection` / `freshness_from_projection` — the projection adapters.
- Value objects: `PermissionBasis`, `SourceRights`, `Freshness`, `ProviderRule`,
  `EmailEligibilityRequest`, `EmailEligibilityDecision`, `EligibilityBlocker`.
- The stable `REASON_*` codes and the `EmailEligibility` enum.

## Tests

`tests/test_email_eligibility.py` pins the behaviour: the reproducible allow, the
verified/high-fit-without-consent block, the independent provider deny, each remaining gate's
fail-closed block, the collected-in-order blockers, and the convenience builders. The
DCA-075 projection → eligibility read path runs over real persisted `ConsentReceipt` rows on
**disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`) — the SQLite variant is a convenience; the
Postgres test skips when no URL is set. The decision itself touches no DB.

## Manual check

```
python scripts/manual_check.py DCA-087
```

Runs three fixtures with **identical deliverability** (same verified + high-fit address, same
clear suppression view, same source rights) in isolated synthetic Businesses A/B:

1. **consented** (an evidenced grant) → `ELIGIBLE`;
2. **flag-only import** (a bare `subscribed=true` that produced no evidenced receipt → no
   permission) → `BLOCKED` on `consent_not_in_force`, *despite* being verified + high-fit;
3. **provider-prohibited** (fully consented, otherwise passing) → `BLOCKED` on
   `provider_restricted`, showing the provider gate denies independently.

Business B's own consented address is unaffected (tenant-isolated inputs). Local synthetic
only; no AI, no paid action, no network, no send; the decision changes no state.
