# DCA-095 — Revalidate immediately before host dispatch

Stage: India channel eligibility · key `dispatch-recheck` · source sections 5, 10.

## What this adds

A service-authenticated **consume/recheck** contract that sits between a DCA-094 activation reference
and a host's actual send. A reference proves the DCA's gates were clear *when it was issued*; state
moves after that. Immediately before dispatching, the host RE-ASKS the DCA — re-validating current
**permission, suppression and version state** — and, on a definitive grant, **consumes exactly one
dispatch slot** idempotently.

A grant records that the DCA re-cleared its OWN blockers for exactly one dispatch. It is **not**
recipient consent and **not** a send authorization — the one send pipeline owns and performs the actual
send, which stays a disabled future capability.

## Design

- **The recheck reuses the accepted mechanics.** It re-verifies the reference against the host's
  CURRENT intended context via the DCA-094 `verify` (`expected` binding), then re-runs the DCA-093
  email eligibility evaluation over the CURRENT gate context. No new policy logic — the authoritative
  checks are the ones already accepted.
- **Three things get re-validated:**
  - **Version state** — the `permission_version` / `source_version` / `campaign_revision` the reference
    was minted under are compared to what the host now holds; any drift rejects the reference as stale
    (`activation_reference_binding_mismatch:<dimension>`).
  - **Permission state** — the current `permissionState` feeds the evaluator; a withdrawal (now
    `withdrawn`/`none`) blocks with `consent_not_in_force`.
  - **Suppression state** — the current authoritative snapshot feeds the fail-closed suppression gate; a
    contact suppressed since issuance blocks (`suppression_blocked`); an unavailable/stale/out-of-scope
    snapshot is REVIEW and fails closed.
- **Idempotent consumption** — `app/dispatch/consumption.py` + the `dispatch_recheck_consumptions`
  table. A grant records exactly ONE row per `(business_id, idempotency_key)`; the unique constraint is
  the serialisation point, so a retried or concurrent consume of the same dispatch replays that one
  grant (consume-once). The row binds a one-way `recipient_digest` and a `reference_fingerprint` (a
  digest of the token) — never the address or the token itself.
- **Grants only persist; denials change nothing.** A blocked/review/rejected recheck writes no row — a
  rejection leaves no state and reproduces exactly. A fresh key after a withdrawal is re-evaluated and
  blocked; a reused key for a *different* dispatch is a `409` conflict.
- **Fail-closed** — no activation-reference signing secret configured ⇒ the whole capability is
  disabled (`dispatch_recheck_disabled`). Tenant-scoped like every DCA service route: a cross-tenant /
  unknown contact reads as `404`, and a consumption can never be read or matched across tenants.

## HTTP contract (`app/api/routes/dispatch_recheck.py`, `X-Service-Auth`)

`POST /v1/tenants/{business_id}/dispatch-recheck/{channel}` — body = the presented `reference`, a
consume-once `idempotencyKey`, and the CURRENT gate context (the DCA-094 issue fields, here carrying the
host's current view: `permissionState` / `suppression` / `provider` + current `providerName` /
`campaignRevision` / `permissionVersion` / `sourceVersion`).

Returns `200` with a uniform decision the host reads off `dispatch.allowed`:

- **Grant** — `dispatch.status = "allowed"`, `consumption.consumed = true` (`replay` tells a fresh grant
  from a retried one), the bound `binding`, and the evidence/versions the recheck confirmed.
- **Denial** — `dispatch.allowed = false` with `dispatch.status` one of `blocked` (a determinate deny,
  e.g. a withdrawal), `review` (an indeterminate/unavailable authority — fail-closed), or
  `reference_rejected` (a tampered/expired/drifted/wrong-tenant reference; `recheck.referenceReason`
  carries the stable code). Nothing is consumed.

Error statuses: `401` (no service auth), `404` (unknown tenant/channel or a contact not in this
business), `422` (`channel_not_enabled`, `dispatch_recheck_disabled`, or body validation), `409`
(`dispatch_recheck_idempotency_key_reused`). Only `email` is wired; other known channels are disabled.

## Acceptance evidence

- **A withdrawal after reference issuance blocks dispatch** — a reference minted while clear is blocked
  when the subject withdraws (permission flip → `consent_not_in_force`) or is newly suppressed
  (`suppression_blocked`), with no consumption (manual check step 3/4, `tests/test_dispatch_recheck_api.py`).
- **Unavailable authority fails closed** — an unavailable suppression snapshot yields `review`, never a
  grant.
- **Idempotent consume-once** — a still-clear recheck grants and consumes one slot; a retry under the
  same key replays the same row (no second consumption). A reused key for a different dispatch is `409`.
- **No unintended state change on denial** — every denial path writes no row; the consumption count is
  unchanged, so each denial reproduces exactly. A grant never carries `authorizesSend: true`.

## Run it

```text
pytest tests/test_dispatch_recheck_api.py
python scripts/manual_check.py DCA-095
```

The PostgreSQL consume-once / isolation test in `tests/test_dispatch_recheck_api.py` runs only when
`DCA_TEST_DATABASE_URL` points at a disposable Postgres (the unique-constraint serialisation needs a
real engine, not SQLite); it skips otherwise.

## Boundary

DCA-only (Python). No AI-service, Web, Admin, or .NET change. One new tenant-owned table,
`dispatch_recheck_consumptions`: on a fresh DB it is created from `Base.metadata` like every other
table, and the dual-path-safe Alembic migration `0036_dispatch_recheck` creates it + its RLS policy on a
database that reached `0035` before the table existed. Acting on a grant (an actual send) stays a
disabled future capability behind the one send pipeline.
