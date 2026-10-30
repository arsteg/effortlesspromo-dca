# DCA-071 — Approve an ambiguous identity merge

Stage: Evidence and identity resolution · key `merge-review` · source sections 6, 7.

## Prerequisite

DCA-070 (`app/services/field_correction.py`, `FieldCorrection`) and the earlier sequence are
implemented. This builds on the DCA-067/068 pure matchers (`app/matching/org_match.py`,
`app/matching/contact_match.py`), the DCA-059/060 org/person observations
(`app/services/org_observation.py`, `app/services/contact_point.py`), and the DCA-019 permission planes
(`app/identity/permissions.py`). Verified present before starting.

## What this adds

A persisted, tenant-owned, **review-only identity-merge** capability — the human ACTION that sits on top
of a pure matcher *conclusion*. The DCA-067/068 matchers only ever return `MATCH` / `NO_MATCH` /
`REVIEW` / `INSUFFICIENT` and merge, transfer and record **nothing** (`auto_merged` is always `False`).
A merge is never an automatic collapse — it is this deliberate, authorized, evidenced record.

- `app/db/models.py` — `IdentityMerge` (tenant-owned, RLS) + `IdentityMergeStatus` enum. One row is one
  reviewer's approval that two of a business's observed identities — two `OrgObservation`s or two
  `PersonObservation`s — are the SAME entity. Stores both subject references (`primary` = surviving,
  `duplicate` = merged-away), the matcher `match_outcome` / `match_confidence` / `evidence` snapshot, the
  reviewer, and a `status` (`active` here; `reversed` is reserved for DCA-072 lineage). Unique per
  `(business_id, subject_type, primary_subject_id, duplicate_subject_id)`.
- `app/services/identity_merge.py` — `IdentityMergeStore` (the single sanctioned writer) with
  `merge(...)` / `evaluate(...)` / `get(...)` / `existing_for_pair(...)`, plus `IdentityReviewer`,
  `IdentityMergeInput`, `MergeEvidence`, `IdentityMergeError` / `IdentityMergeAuthorizationError` and
  stable refusal codes.
- `app/identity/permissions.py` — new business-plane write scope `Scope.IDENTITY_MERGE`
  (`"identity.merge"`), granted to acting users (`user`/`member`) and administrators (`admin`/`owner`),
  NOT a read-only `viewer`.
- `migrations/versions/0041_identity_merges.py` — the dual-path create+RLS migration (same guard style
  as 0028/0032/0033).

No HTTP route, no AI capability, no acquisition/send/promote path. **No .NET / UI / AI-service changes.
No new dependency.**

## The load-bearing guarantees (the DCA-071 acceptance criteria)

- **Only reviewed COMPATIBLE identities merge.** `merge()` re-runs the relevant matcher over the two
  STORED observations (never trusting a caller-asserted conclusion) and refuses unless the outcome is a
  `MATCH` or a `REVIEW` candidate (the "ambiguous" merge a human approves). A `NO_MATCH` (a conflicting
  typed registration identifier — a shared name can never override it) or an `INSUFFICIENT` pair (no
  linking evidence) is refused with `incompatible_identities` before the session is mutated.
  > Within one business two distinct observations can never share a strong identifier or a personal
  > contact value (both are unique per business), so a same-business merge is inherently the AMBIGUOUS
  > `REVIEW` case — exactly what this story approves.
- **Unrelated consent scopes remain SEPARATE.** The store links IDENTITIES only; it never creates,
  modifies or transfers any `ConsentReceipt` or `ProcessingRestriction`. Merging identity is not merging
  consent, suppression or restrictions (mirrors the DCA-068 permission-never-transfers guarantee).
- **Authorized by a HUMAN reviewer; an AI suggestion can never execute a merge.** Authority is checked
  FIRST: a non-human (`service`/`system`) actor is refused with `identity_merge_ai_actor_forbidden`; a
  human lacking `identity.merge` (a viewer, a non-member, a platform operator) is refused with
  `identity_merge_not_authorized` — before any state is read or written.
- **Business-private, tenant-scoped, idempotent.** Both subjects resolve WITHIN the business (an unknown
  or cross-tenant id reads as `subject_not_found`); a by-id read of another business's merge is
  not-found; re-approving an already-merged pair (either direction) returns the existing row — no
  duplicate, no second audit entry.

Each success stages an atomic DCA-026 audit entry in the SAME unit of work; references stay OPAQUE (ids /
subject kind / match outcome / signal count) — never the evidence signal details or any raw value.

## Future capabilities kept disabled

Acting on a merged identity downstream — collapsing observations, combining records, sending — stays a
disabled future capability. **Reversing a merge is the SEPARATE next story (DCA-072)**; this story records
only `active` merges and stores enough lineage (both subjects, direction, evidence, reviewer) for DCA-072
to reverse by flipping `status`.

## Tests / manual

- `tests/test_identity_merge.py` — success (ambiguous REVIEW merge recorded), the denials
  (viewer / platform-operator / AI-actor refused; conflicting-identifier `NO_MATCH` and `INSUFFICIENT`
  refused; self-merge refused), consent-NOT-combined, cross-tenant not-found, and idempotency. Store
  logic on in-memory SQLite; the real transaction/RLS path on a disposable PostgreSQL
  (`DCA_TEST_DATABASE_URL`). RLS/migration coverage in `tests/test_rls.py` / `tests/test_migrations.py`.
- `python scripts/manual_check.py DCA-071` — inspectable scenario on isolated synthetic Businesses A/B.
