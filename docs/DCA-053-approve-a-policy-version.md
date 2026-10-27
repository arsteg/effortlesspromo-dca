# DCA-053 — Approve a policy version

Stage: Source rights and policy foundation · key: `policy-review`

## What this adds

A reviewed-policy **lifecycle** over the DCA-052 compliance policy rulepack. A **platform operator**
records a review of one rulepack version carrying the four essentials — the **references** relied on,
the **reviewer**, the **review date** and an explicit **review expiry** — and the endorsed version is
promoted from `draft` to `active`. The service **authors no legal conclusion**: the operator cites the
external basis (references) and the row records it; nothing is interpreted automatically.

This is the policy analog of DCA-046 (approve a source configuration), on the platform control plane.

A `CompliancePolicyApproval` row (one approval of one rulepack version) carries:

- **jurisdiction** — always `IN` (mirrors the rulepack it endorses).
- **policy_version** — the DCA-052 version this approval endorses (validated to exist).
- **reviewer_id / reviewer_type** — WHO approved: an opaque *platform operator* subject id.
- **review_references** — `{reference, kind, uri}`; a non-empty `reference` is required (a bare
  approval is refused). The approval cites a basis; it never synthesizes a legal conclusion.
- **reviewed_at** — the review date (required).
- **effective_from / expires_at** — the approval's explicit validity window; `expires_at` (the review
  expiry) is required. An approval lapses rather than living forever.
- **content_fingerprint** — a stable SHA-256 digest of the rulepack's normalized rules at approval
  time, so the approval is bound to the **exact** content reviewed.
- **status** — `active` (an approval this story writes) vs `expired` (a lapsed approval; the scheduled
  sweep is a later story — until then a lapse is detected by the explicit window).

### The guarantees (acceptance criteria)

- **Draft/expired packs never authorize production behavior.** `PolicyApprovalStore.evaluate(version)`
  is a *lifecycle* check (it never interprets the rules — the policy engine stays a disabled future
  capability): a version is in force only when it is approved, `active`, and `now` falls inside the
  approval's window. A draft (unapproved) version is denied `not_approved`; a lapsed approval is denied
  `approval_expired`; a not-yet-effective approval is denied `approval_not_yet_effective`.
- **Changing approved content requires a new version.** The DCA-052 store is insert-only, so editing
  approved content records a **new** rulepack version — a fresh `draft`, unapproved until separately
  reviewed. The prior approval's `content_fingerprint` still binds the exact rules it reviewed, so an
  approval can never silently follow an edited pack. A version is approved at most once
  (`already_approved`, over a unique `(jurisdiction, policy_version)` constraint).
- **Approval is a PLATFORM-operator action.** Authority is the platform-plane scope
  `platform.policy.rulepack.approve`. A tenant administrator holds only business-plane scopes, so the
  approval is refused (`not_platform_operator`) **before** any state is read or written.
- **Incomplete reviews are rejected before anything is staged** (stable codes): an unknown version
  (`policy_version_not_found`), a missing reviewer / references / review date / expiry
  (`missing_reviewer`, `missing_review_references`, `missing_review_date`, `missing_expiry`), or an
  expiry at/before the review (or effective) date (`invalid_expiry`). A refusal leaves no row and no
  promotion.

## Where it lives

- Store + validation + lifecycle evaluation: `app/services/policy_approval.py`
  (`PolicyApprovalStore`, `PolicyApprovalReview`, `PolicyReviewerPrincipal`,
  `PolicyAuthorizationDecision`, `content_fingerprint`, stable `REASON_*` codes).
- Model + enums: `app/db/models.py` (`CompliancePolicyApproval`, `PolicyApprovalStatus`; `EXPIRED`
  added to `PolicyRulepackStatus`).
- Platform scope: `app/identity/permissions.py` (`Scope.POLICY_RULEPACK_APPROVE`, granted to
  `platform_operator`).
- Migration: `migrations/versions/0022_compliance_policy_approvals.py` (control-plane table; **not**
  tenant-RLS — a shared policy registry, like the source registries and `compliance_policy_rulepacks`).

## Boundary

Platform **control-plane** data — a shared policy registry, never tenant-scoped, outside tenant RLS.
It holds only the review record (opaque reviewer id + references + a content digest): no prospect
contact data, no secrets. No UI, no AI-service change, no `.NET` change. The policy **engine** that
would consult an in-force pack's rules to permit/deny an action stays a **disabled** future capability;
promoting a version authorizes no production behavior on its own.

## Developer manual test

Disposable PostgreSQL covers the schema/transaction paths; the store logic (control-plane, no RLS) is
unit-tested on in-memory SQLite — the same split every source-rights story uses.

```bash
# unit: approve / evaluate / draft-denied / expired / new-version / tenant-admin / incomplete (SQLite)
pytest tests/test_policy_approval.py -q

# schema + migration + RLS-exclusion on a disposable PostgreSQL
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55453:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55453/dca"
pytest tests/test_migrations.py tests/test_rls.py -q

# inspectable scenario (prints real rows/decisions, not PASS/FAIL)
python scripts/manual_check.py DCA-053
# …or against the disposable Postgres (control-plane transactions):
python scripts/manual_check.py DCA-053 \
  --database-url "postgresql+psycopg://postgres:dca@localhost:55453/dca"
```

`manual_check.py DCA-053` records a rulepack draft, evaluates the unapproved version (→ denied
`not_approved`), refuses a tenant-administrator approval (→ `not_platform_operator`, no state change)
and an incomplete review (→ `missing_review_references`, nothing staged), then has a platform operator
approve it (→ promoted `draft`→`active`, in force), and finally modifies the content into a new draft
version (→ v1 stays in force, v2 is unapproved, and v1's fingerprint still binds v1's content).
