# DCA-072 — Reverse a reviewed identity merge

Stage: Evidence and identity resolution · key `merge-undo` · source sections 6, 7.

## Prerequisite

DCA-071 (`app/services/identity_merge.py`, `IdentityMerge` + `IdentityMergeStatus`, scope
`Scope.IDENTITY_MERGE`, migration `0041_identity_merges.py`) and the earlier sequence are implemented.
Verified present before starting — the `reversed` status and the full merge lineage (both subject refs,
direction, evidence, reviewer) were already persisted by DCA-071, so this story needs **no new columns and
no migration**; it reuses them.

## What this adds

The human ACTION that **undoes** a prior reviewed merge — `IdentityMergeStore.reverse(...)`. It flips an
`active` merge's `status` to `reversed`, restoring the two identities to their SEPARATE / unmerged state
(the active link no longer stands), while preserving all history.

- `app/services/identity_merge.py` — new `IdentityMergeStore.reverse(merge_id, reviewer, *, reason="",
  now=None)` method; new audit action `AUDIT_ACTION_REVERSE_MERGE` (`"identity.merge.reverse"`); new stable
  refusal codes `REASON_MERGE_NOT_FOUND` (`"identity_merge_not_found"`) and `REASON_ALREADY_REVERSED`
  (`"identity_merge_already_reversed"`). Reuses the DCA-071 `IdentityReviewer` authority checks
  (`is_human_reviewer()` / `has_merge_scope()`), error types and the atomic `AuditSink`.

No model change, no migration, no HTTP route, no AI capability, no acquisition/send/promote path.
**No .NET / UI / AI-service changes. No new dependency.**

## The load-bearing guarantees (the DCA-072 acceptance criteria)

- **Undo restores separate identity links + preserves history.** `reverse()` flips `active → reversed`,
  so the pair's active link no longer stands (the identities are separate again). The merge row is NEVER
  deleted and the original DCA-071 `identity.merge.record` audit entry is never touched — a SEPARATE
  `identity.merge.reverse` audit entry is appended (opaque references only: ids / subject kind / outcome /
  the `previousStatus → newStatus` transition), so the full recorded→reversed trail is readable.
- **Does NOT resurrect revoked consent.** Because the merge never combined or transferred any
  `ConsentReceipt` / `ProcessingRestriction` (DCA-071), its reversal has nothing to unwind on those tables
  and `reverse()` never touches them. A consent withdrawn between the merge and the reversal stays
  withdrawn; no permission is re-granted or recombined. Reversal moves identity state only.
- **Same authority as the merge; AI / unauthorized refused (the relevant denial).** Authority is checked
  FIRST: a non-human (`service`/`system`) actor is refused with `identity_merge_ai_actor_forbidden`; a
  human lacking `identity.merge` (a viewer, a non-member, a platform operator) is refused with
  `identity_merge_not_authorized` — before any state is read or written.
- **Not-reversible is refused, changing nothing.** An unknown id — and another tenant's merge id — read as
  not-found (`identity_merge_not_found`); an already-reversed merge is refused (`identity_merge_already_
  reversed`) because there is nothing left to undo, so a double-reversal changes nothing.

The flip and its audit entry are staged into the caller's session (atomic DCA-026 — they commit together);
the caller owns the unit of work. Acting on the (un)merged identity downstream stays a disabled future
capability.

## Tests / manual

- `tests/test_identity_merge_reversal.py` — the undo (status flip + separation + preserved record-audit +
  appended reverse-audit), the denials (viewer / platform-operator / AI-actor refused; unknown & cross-
  tenant not-found; double-reversal refused), and the consent guarantee (a consent withdrawn after the
  merge is NOT resurrected by the reversal). Store logic on in-memory SQLite; the real transaction/RLS
  path on a disposable PostgreSQL (`DCA_TEST_DATABASE_URL`).
- `python scripts/manual_check.py DCA-072` — inspectable scenario on isolated synthetic Businesses A/B:
  record two merges, undo one, attempt the denials, and prove a consent withdrawn between merge and
  reversal stays withdrawn. Prints the actual rows/decisions.
