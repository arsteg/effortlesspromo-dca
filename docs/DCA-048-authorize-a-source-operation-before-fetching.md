# DCA-048 — Authorize a source operation before fetching

Stage: Source rights and policy foundation · key: `source-preflight`

## What this adds

A single, deterministic **preflight gate** (`SourcePreflight`) a would-be acquisition consumer must
clear BEFORE it ever touches a source adapter. Given a **business, action, field, purpose, time and
source status**, it returns one `PreflightDecision` (`allowed` + a stable `reason`), composing the
rights foundation already built — it introduces no new policy of its own, it makes the
authorize-before-fetch decision explicit, reproducible and auditable.

The gate layers three existing stores into one decision, evaluated deny-by-default in a FIXED order
and stopping at the first failure:

1. **request shape** — a `source_key`, a recognized `operation` (a `SourceOperation`) and a `purpose`
   are all required (`missing_source_key`, `unknown_operation`, `missing_purpose`).
2. **source status** (DCA-044) — the catalog entry must exist (`source_not_found`) AND be usable, i.e.
   an approved, ENABLED source (`source_not_enabled`). Nothing in the service promotes a source to
   ENABLED, so in the running system this gate denies every fetch — acquisition stays disabled.
3. **platform ceiling + time** (DCA-045/046) — an in-force `SourceTermsApproval` (active AND inside
   its validity window at `now`) must grant the action on the field, and its terms must document the
   purpose (`source_not_platform_approved`, `operation_denied_at_platform`,
   `purpose_denied_at_platform`). The action/field decision reuses DCA-047's `permits` intersection.
4. **business narrowing** (DCA-047) — the tenant's in-force entitlement must be enabled and grant the
   action on the field and the purpose (`source_not_entitled`, `operation_not_entitled`,
   `purpose_not_entitled`).

### The guarantees (acceptance criteria)

- **A denied preflight makes ZERO adapter calls, and official origin or robots permission alone can
  never bypass rights.** The ONLY sanctioned path from an acquisition consumer to a source adapter is
  `SourcePreflight.fetch(adapter, request, icp, now=...)`, which authorizes FIRST and invokes
  `adapter.fetch` only when the decision is `allowed`. A denial returns immediately with
  `adapter_called=False` / `candidates=None`. The request's advisory `origin_signals` (e.g. an
  "official origin" trust tier or a `robots.txt` that permits crawling) are recorded on the decision's
  `evidence` for audit but are **never consulted** by `authorize` — so an official registry or a
  permissive `robots.txt` can never, on its own, authorize a fetch the rights chain withholds.
- **Reproduce the result and relevant denial without unintended state changes.** `authorize` is a
  pure, side-effect-free read: it stages nothing, commits nothing and calls no adapter, so a denial
  (or an allow) is perfectly reproducible and re-running it changes no state. The gate is bound to one
  verified business scope (like the DCA-047 entitlement store), so a tenant's preflight can only
  consult that tenant's entitlements.
- **Future capabilities stay disabled.** This story enables no source, ships no live adapter, and
  adds no table/migration. The catalog's ENABLED state stays a disabled future capability, so every
  production preflight denies at the source-status gate — the gate authorizes no acquisition on its
  own.

## Components touched (DCA only)

- **Service**: `app/services/source_preflight.py` → `SourcePreflight` (the gate, bound to one business),
  `PreflightRequest`, `PreflightDecision`, `PreflightFetchResult`, and the stable reason codes. Pure
  read; composes `SourceCatalogStore` (DCA-044), the in-force approval ceiling (DCA-046) and
  `BusinessSourceEntitlementStore.permits` (DCA-047).
- **Store (DCA-047)**: `app/services/source_entitlement.py` → two public read helpers
  (`in_force_approval`, `approved_purposes`) so the preflight can resolve the platform ceiling and
  its documented purposes without reaching into private methods. No behaviour change.
- **Manual check**: `scripts/manual_check.py` → `DCA-048` scenario.

No new table, migration, HTTP surface, Web/Admin UI or AI-service change is in scope: a business- or
worker-facing API/UI to drive a fetch is a later story, kept disabled here ("missing contracts stay
disabled"). The gate reuses the existing rights schema only.

## Stable codes

Request-shape refusals: `missing_source_key`, `unknown_operation`, `missing_purpose`.
Source-status refusals: `source_not_found`, `source_not_enabled` (reused from DCA-044).
Platform-ceiling refusals (reused from DCA-047): `source_not_platform_approved`,
`operation_denied_at_platform`, `purpose_denied_at_platform`.
Business-narrowing refusals: `source_not_entitled`, `operation_not_entitled` (reused from DCA-047),
`purpose_not_entitled` (new — a purpose documented at the platform but narrowed out by the business).

## Tests

- `tests/test_source_preflight.py` — the behaviour + failure cases (a fully-authorized operation
  ALLOWS and reaches a COUNTED fake adapter; every denial — no entitlement, operation/purpose denied
  at platform or not entitled, an unapproved/draft/unknown source — makes ZERO adapter calls; an
  official-origin + robots-allowed source with no rights is still denied; the decision is per-business
  and re-running it changes nothing).
- `tests/test_manual_check.py` — runs the `DCA-048` scenario end-to-end on synthetic SQLite and
  asserts the allow reaches the adapter while every denial makes zero adapter calls.

No disposable-PostgreSQL test is required: this story adds no table, migration or RLS policy (it is
pure read logic over the existing DCA-044/045/046/047 schema), so there is no new DDL/locking/RLS path
to exercise. The underlying rights tables' transaction/RLS/migration paths remain covered by
`tests/test_rls.py` and `tests/test_migrations.py`.

## Developer manual test

Isolated synthetic Businesses A/B. A source is catalogued (DCA-044), a terms version recorded
(DCA-045) and approved (DCA-046) so it is the in-force platform ceiling (collect/store/display/export
source-wide — `share` DENIED; email display-only; purpose `prospecting`). For the ALLOW path only, the
catalog entry is synthetically marked ENABLED (the service never does this — a test-only enable so the
allow branch is reachable). Business A entitles a SUBSET (DCA-047): collect/display, purpose
`prospecting`.

```
python scripts/manual_check.py DCA-048
```

Expected: a fully-authorized request (`collect` / `prospecting`, carrying an "official origin +
robots-allowed" hint) is **allowed** and reaches the counted fake adapter exactly once. Every
prohibited request — `export` (approved at platform but not entitled), `share` (denied at platform),
purpose `resale` (not documented), an **official-origin UNAPPROVED** source, a DRAFT (not enabled)
source, and an unknown source — is **denied** with its stable reason and makes **zero** adapter calls
(total adapter calls across all denials = 0). The UNAPPROVED source is itself official-origin with a
permissive robots.txt yet is still denied `source_not_platform_approved`, proving origin/robots cannot
bypass the rights chain. Business B (same platform ceiling, no entitlement) is denied
`source_not_entitled`; A's decision re-runs identically and the preflight stages nothing (entitlement
rows stay 1; the only audit action is the DCA-047 entitlement set). Local synthetic only; no AI/paid
action; the preflight authorizes no acquisition.
