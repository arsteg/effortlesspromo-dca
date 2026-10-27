# DCA-035 — Protect mutation requests with idempotency keys

**Status:** implemented. Adds tenant/route-scoped **request idempotency** for the DCA's write
routes: a mutation carrying an `Idempotency-Key` header is run at most once — a byte-identical retry
replays the first response and performs no second state change; the same key reused for a *different*
body is rejected; and a key never collides across tenants or routes. Backend-only; no provider
SDKs/keys, no model routing, no second AI gateway, no writes to another service's database. Future
capabilities stay disabled.

## What it does

A client (the platform, on behalf of an already-authorized user) may make a write retry-safe by
sending an `Idempotency-Key` header, exactly like the platform's own BR-124 idempotency. The first
call runs the handler and stores its outcome; any identical retry replays that stored outcome.

| Behaviour | How |
|---|---|
| **Same key + same body ⇒ same result** | The first call's `(status, body)` is stored in `idempotency_records` keyed by `(business_id, method, route, idempotency_key)`. A retry whose body hashes to the same SHA-256 digest replays the stored response verbatim (header `Idempotent-Replay: true`) — the handler does not run, so **no second state change** occurs. |
| **Same key + different body ⇒ rejected** | A stored record with a *different* `request_digest` is a key reuse → `409 idempotency_key_reuse`. The rejection happens on lookup, before any write, so it changes nothing. |
| **Keys do not collide across tenants** | The unique constraint and every lookup/insert are scoped to `business_id`. Two businesses — or two routes — may use the same key and neither constrains the other. |
| **Atomic with the change** | The record is staged into the *caller's* session (like the DCA-026/027/028 sinks) and committed in the **same** transaction as the business change. A concurrent duplicate loses the unique-constraint race, rolls back its own duplicate change, and replays the winner's result — so the effect is applied exactly once. |
| **Opt-in** | A request with no `Idempotency-Key` is served normally and records nothing — idempotency is a protection a client requests per write. |

## Where it lives

- **Store** — `app/idempotency/store.py`: `IdempotencyStore` (tenant-scoped, caller-atomic reader/
  writer), `request_digest()` (SHA-256 of the raw request body), `IdempotencyConflict`,
  `IDEMPOTENCY_HEADER`.
- **Model** — `app/db/models.py`: `IdempotencyRecord(TenantOwned)` with
  `tenant_unique("method", "route", "idempotency_key")`. Tenant-owned, so it inherits the DCA-017
  `business_id` column and the DCA-018 RLS policy like every other tenant data table.
- **HTTP seam** — `app/api/idempotency.py`: the `idempotency` FastAPI dependency (replays/rejects a
  known key *before* the handler runs), `commit_idempotent` (stages the record and commits it
  atomically with the change, reconciling a lost race), and `install_idempotency` (the
  `IdempotencyReplay → response` handler). Shares the handler's session via `Depends(get_session)`.
- **Routes** — `app/api/routes/tenants.py`: the write routes `register`, `enable`, `disable`,
  `icp`, `config`, `candidates/accept`, `candidates/dismiss` go through it. `discovery/run` is
  intentionally **not** wired here: it owns its own transaction/commit and is already entitlement-
  gated; making it atomically idempotent would require refactoring its commit (a separate change).
- **Migration** — `migrations/versions/0009_idempotency_records.py`: dual-path (metadata baseline on
  a fresh DB; explicit create + RLS on a DB that predates the table), same guard style as 0005–0008.

## Scope resolution

The tenant scope for a key is: the path `business_id` when the route has one, else a `businessId`
field in the JSON body (the `register` route carries it there), else a `__platform__` sentinel (no
current route reaches it — it only keeps the store's non-null scope honest).

## Privacy

Only opaque control fields of the route's own response are stored (ids / statuses / flags), never
contact data — the DCA route responses here carry none.

## Tests

- `tests/test_idempotency_store.py` — always-on SQLite unit tests (digest stability, reconcile
  new/replay/conflict, cross-tenant and cross-route non-collision, duplicate-key constraint, input
  validation) + **disposable PostgreSQL** atomicity (rollback removes the record) and the concurrent
  duplicate losing the unique-constraint race.
- `tests/test_idempotency_api.py` — the acceptance criteria end to end over HTTP (same key/body
  replays with no second state change; same key/different body → 409 with no change; same key across
  tenants does not collide; `register` keyed by its body scope; no-key served normally).
- `tests/test_migrations.py` / `tests/test_rls.py` — `idempotency_records` added to the migrated-set
  and RLS-covered-set assertions; a 0009 pre-existing-DB create-branch test.

## Manual check

```
python scripts/manual_check.py DCA-035
```

Registers A and B, PUTs A's config with a key, repeats it (replay, no second change despite an
out-of-band write in between), reuses the key with a different body (409), then reuses it for B (B's
own result). Prints each real response/body. Local synthetic only; no AI/paid action.
