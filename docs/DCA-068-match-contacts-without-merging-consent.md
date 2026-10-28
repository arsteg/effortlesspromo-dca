# DCA-068 — Match contacts without merging consent

Stage: Evidence and identity resolution · key `contact-match` · source sections 6, 7.

## Prerequisite

DCA-067 (`app/matching/org_match.py`, `match_organizations`) and the earlier sequence are implemented
and were used as building blocks: DCA-066 `app/normalization/phones.py` (E.164), DCA-064
`names.py` / `domains.py`, DCA-065 `org_identifiers.py`, the DCA-060 `ContactPointStore` /
`PersonObservation` + `ContactPoint`, and the DCA-059 `OrgObservationStore`.

## What this adds

A pure, deterministic, network-free **person identity-resolution decision** — the contact-level
sibling of the DCA-067 organization matcher. It compares two bundles of already-normalized **contact**
evidence (a name, email / phone contact points, an employer organization, a role) and decides whether
they are the **same person**, using **strong personal evidence first**. It records nothing, mutates
nothing, **never merges** two observations, and — the load-bearing guarantee of this story — **never
transfers one person's permission/consent to another** (`auto_merged` and `permission_transferred`
are always `False`).

- `app/matching/contact_match.py` — the matcher:
  - `match_contacts(a, b) -> ContactMatchResult`
  - `ContactMatchSubject` + `build_contact_subject(...)` (from raw strings, via the existing
    normalizers) + `subject_from_person_observation(contact_store, person, *, org_store=None)`
    (read-only, tenant-scoped, from a stored DCA-060 `PersonObservation` + its contact points and
    linked employer observation)
  - `ContactMatchOutcome` (`match` / `no_match` / `review` / `insufficient`), reusing `Confidence`
  - `ContactMatchSignal` / `ContactSignalKind` (reusing `SignalStrength` / `SignalRelation`), and
    `ContactMatchDenial`
- `app/matching/__init__.py` — extended package surface (org + contact matchers).
- `app/normalization/errors.py` — two new stable email refusal codes (`empty_email`, `invalid_email`)
  used by the contact matcher's internal email normalizer.

No DB table, no migration, no HTTP route, no AI capability, no acquisition/send/merge/consent path.
**No .NET / UI / AI-service changes. No new dependency.** It reuses `app/normalization/*`
(names/domains/phones) and `app/matching/org_match.py` (employer comparison, DCA-067).

## The evidence hierarchy (why people are matched more conservatively than organizations)

A person carries *permission* (consent, suppression, do-not-contact) that must never leak to the wrong
human, so the thresholds are stricter than DCA-067.

| Evidence | Strength | Effect |
|---|---|---|
| Shared **personal** email mailbox (per-person address) | strong | `MATCH` — same person (unless employment/role changed, below) |
| Shared personal mailbox **+ different employer org / different role** | strong anchor, conflicting context | `REVIEW` — a reassignment / role change to review, never a silent carry-over |
| Shared **role / functional** mailbox (`info@`, `sales@`, …) | weak | `REVIEW` candidate only — many people answer it |
| Shared / recycled **phone number** (E.164) | weak | `REVIEW` candidate only — carriers recycle, desks/households share |
| Common **name** (DCA-064 `match_key`) | weak | `REVIEW` candidate only |
| Weak overlap **+ different personal mailboxes** on each side | strong distinguishing | `NO_MATCH` — two different people on a recycled/shared contact point |
| Same / different **employer org** (via DCA-067) | weak | corroborates (caps at `medium`) or contradicts a candidate; never decides alone |

`REVIEW` confidence: one weak anchor → `low`; two (e.g. phone **and** name) → `medium`; an employer
corroboration bumps `low`→`medium` (capped at `medium`); an employer/role conflict knocks it back to
`low`. An employer signal with no personal/weak anchor is too weak to propose a candidate →
`INSUFFICIENT`.

Only a per-person email mailbox participates as the strong anchor. A role/functional mailbox and every
phone channel are treated as weak, shareable contact points — never proof of a person.

## Acceptance criteria → evidence

- **A shared/recycled number or common name never transfers a person's permission to someone else.**
  A shared phone alone, a common name alone, or a shared role mailbox alone is a `REVIEW` candidate —
  never a `MATCH` (`test_shared_number_alone_is_a_review_candidate_not_a_match`,
  `test_common_name_alone_is_a_review_candidate_not_a_match`,
  `test_shared_role_mailbox_alone_is_a_review_candidate_not_a_match`). When two people share a
  recycled/shared number but carry **different personal mailboxes**, the matcher concludes they are
  different people (`NO_MATCH`) and nothing is merged or transferred
  (`test_recycled_number_with_different_personal_mailboxes_is_no_match`). Only a shared **personal**
  mailbox confirms a `MATCH` (`test_shared_personal_mailbox_is_a_match`). A reassignment / role change
  with the same personal mailbox is flagged for review, never carried over
  (`test_same_person_different_employer_is_a_reassignment_review`,
  `test_same_person_different_role_is_a_role_change_review`). `auto_merged` and
  `permission_transferred` are asserted `False` throughout.

- **Reproduce the result and the relevant denial without unintended state changes or later-story
  work.** The matcher is pure: the same inputs give the identical, symmetric result every time, and
  the relevant denial — `NO_MATCH` on a recycled number with different personal mailboxes, and the
  refusal to collapse a shared number/name to anything stronger than `REVIEW` — is perfectly
  reproducible with no state to change (`test_result_is_reproducible_and_symmetric`). A malformed
  input is collected as a `ContactMatchDenial` and never fabricates a match
  (`test_build_contact_subject_collects_denials_without_raising`,
  `test_malformed_email_does_not_fabricate_a_match`); an ambiguous (no-country) phone is dropped, not
  a denial, and never an anchor (`test_unresolved_phone_is_dropped_not_a_denial_and_never_an_anchor`).
  The read-only observation adapter stays tenant-scoped
  (`test_adapter_matches_same_person_via_each_tenants_own_store`,
  `test_adapter_recycled_number_keeps_separate_identities_with_unchanged_permissions`,
  `test_adapter_builds_employer_and_flags_reassignment`).

## Tests / quality

- `tests/test_contact_match.py` — 20 focused tests (personal-mailbox match, recycled-number no-match,
  weak-only review for number/name/role-mailbox, reassignment & role-change review, employer
  corroboration/conflict, insufficient, denial collection, unresolved-phone drop,
  reproducibility/symmetry, and the read-only tenant-scoped observation adapter).
- The matcher is pure Python — there is no DB / RLS / transaction in the decision, so no PostgreSQL
  fixture is required for it (the disposable-Postgres rule applies to DB-touching behavior; the
  observation adapter's underlying `person_observations` / `contact_points` / `org_observations`
  transaction + RLS path is already covered on disposable Postgres by `tests/test_contact_point.py`,
  `tests/test_org_observation.py` and `tests/test_rls.py`). Those Postgres-gated suites are the 60
  skips when `DCA_TEST_DATABASE_URL` is unset locally.
- Full suite re-run green: `937 passed, 60 skipped`.
- Gate on the touched files: `ruff check app/matching app/normalization/errors.py
  tests/test_contact_match.py scripts/manual_check.py`, `ruff format --check …`, `mypy app/matching`
  all green. (`mypy` over the whole repo surfaces the same 16 **pre-existing** errors in
  `app/compliance/policy_rulepacks.py` and earlier `scripts/manual_check.py` scenarios — none in this
  story's code.)

## Manual check

```text
python scripts/manual_check.py DCA-068
```

Runs shared-personal-mailbox, recycled-number, shared-number, common-name, shared-role-mailbox,
reassignment, role-change and no-evidence fixtures through the matcher and prints each pair's outcome,
confidence, reassignment flag, `auto_merged` / `permission_transferred`, and contributing signals: a
shared personal mailbox confirms a `MATCH`; a recycled number with different personal mailboxes yields
`NO_MATCH`; a shared number / common name / role mailbox alone is only a `REVIEW` candidate; a
reassignment / role change is a `REVIEW` flag; and the result + the relevant denial reproduce with no
state change. Local synthetic only; no AI/paid/network/DB write, and nothing is ever merged and no
permission is ever transferred.
