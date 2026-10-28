# DCA-083 — Resolve a privacy request's affected scope

Stage: *Consent and restriction authority*. Scope: an **operator-reviewed scope resolution step** that
identifies the **records**, the **controllers/fiduciaries** and the **permissible actions** a privacy
request reaches — and **executes nothing** (no deletion, no send, no model call, no state change).
Acting on the resolved scope stays a disabled future capability.

## What was built (DCA only)

A single read-only resolver + a thin read-only HTTP contract. No new table, migration or RLS surface
(the resolver only reads existing tables).

- **Service** `app/services/privacy_scope.py::PrivacyScopeResolver` — two resolution planes mirroring
  the two privacy-request kinds:
  - `resolve_local(grievance, principal)` — a LOCAL grievance (DCA-082). Bound to the grievance's
    business, it identifies ONLY that business's records that pertain to the requester (matched by the
    one-way contact digest — never a raw contact), names that single business as the `data_fiduciary`,
    and derives the permissible action from the category. A request for one tenant **never** selects
    another tenant's records. Platform-controlled source scope a local operator cannot resolve is
    surfaced as `unresolved_authority` (`needs_review=True`), and a grievance with no subject contact
    flags `subject_contact_not_provided`.
  - `resolve_platform_source(restriction, principal)` — a platform-wide source removal (DCA-078). Keyed
    on the control-plane source, it identifies EVERY tenant's records acquired from the removed source
    and names the platform `source_controller` + each affected tenant as a `data_fiduciary`.
  - `ScopeResolution.actions_executable` is **always `False`** — resolution identifies scope and
    executes nothing, so even an erasure request only NAMES `erase_personal_data` and deletes nothing.
- **Authority** — `resolve_local` requires the business-plane `grievance.read` scope (a read-only
  viewer / non-member / platform operator is refused); `resolve_platform_source` requires the
  platform-plane `platform.source.restrict` scope (a tenant administrator is refused) and cross-checks
  the restriction's authority plane. Each check runs BEFORE any state is read, so a relevant denial
  reads nothing and changes nothing.
- **Routes** `app/api/routes/privacy_scope.py` (mounted in `app/main.py`), both side-effect-free GETs:
  - `GET /v1/tenants/{business_id}/grievances/{id}/affected-scope` — operator local-scope read
    (`grievance.read`; cross-tenant/unknown id → 404).
  - `GET /v1/platform/source-removals/{source_key}/affected-scope` — platform-operator read
    (`platform.source.restrict`; unknown/not-removed source → 404).

### Acceptance criteria → where they hold

- *A request for one tenant does not automatically select every tenant's relationship; unresolved
  authority needs review* — `resolve_local` filters records to the grievance's `business_id` only;
  a subject present in another tenant is never selected. Where a matched record came from a
  platform-controlled source, that platform-wide scope is left `unresolved_authority`
  (`needs_review=True`) for a platform operator to resolve separately — a local operator has no
  authority over it. The platform-wide set is only reachable via the platform-plane
  `resolve_platform_source` (operator-gated).
- *Reproduce the result and relevant denial without unintended state changes or later-story work* — the
  resolver is pure read (no flush/commit/transaction/write); the relevant denials (viewer/non-operator
  → authorization error, cross-tenant/unknown → 404, plane mismatch → error) change nothing, and no
  action is executed (no deletion).

## Tests / quality

- `tests/test_privacy_scope.py` — service unit (SQLite) + HTTP (`TestClient`) + a disposable-Postgres
  test (`DCA_TEST_DATABASE_URL`) proving, on real Postgres transactions, that the SAME subject in A and
  B yields a local resolution for A selecting only A's record while the platform-source resolution spans
  both. Failure cases covered: viewer/platform-operator/tenant-admin denials, cross-tenant/unknown 404s,
  plane mismatch, no-subject-contact review flag, and no-state-change (nothing deleted).
- Quality gate: `ruff check .` · `ruff format --check .` · `mypy` · `pytest` (the resolver modules are
  lint/format/type clean; pre-existing `scripts/manual_check.py` and `policy_decision_record.py`
  findings are untouched — not this story's work).

## Manual check

```text
python scripts/manual_check.py DCA-083
```

Seeds the SAME synthetic subject into Business A and B (both from one platform source), resolves the
LOCAL grievance for A (selects only A's record, names A the fiduciary, identifies the `access` action
WITHOUT executing it, flags the platform-controlled scope as needs-review) vs the PLATFORM-SOURCE
removal (spans both tenants), shows a read-only viewer is denied, and confirms no record was changed —
local synthetic only, no AI/paid action, no deletion/execution.

## Boundary

Python DCA only. No `.NET`, no AI-service, no Web/Admin changes. No new provider SDK/key, model routing
or second gateway. No live source/send/erasure path — resolution executes nothing; acting on the scope
stays a disabled future capability.
