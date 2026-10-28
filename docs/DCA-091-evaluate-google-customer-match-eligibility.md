# DCA-091 — Evaluate Google Customer Match eligibility

Stage: **India channel eligibility** · key: `google-policy` · source sections 5, 10.

## What this adds

One pure, deterministic decision function — `app/compliance/customer_match_eligibility.py` — that
answers a single question for the **Google Customer Match** channel: *may this customer-list audience
be uploaded to Customer Match?* It composes the **two concern-families** Google's Customer Match policy
keeps separate, and preserves each member's **origin through SHA-256 hashing**. It introduces no new
acquisition, no upload, no AI call, no DB write, and is wired into no pipeline/worker/HTTP path.

### A) Data origin — first-party **only** (evaluated per member, fail-closed)

| Gate | Input | Blocks when | Reason code |
|------|-------|-------------|-------------|
| Member origin | `HashedMember.origin` | the audience is empty | `empty_audience` |
| Member origin | `HashedMember.origin` | **any** member is not `first_party` (bought / discovered / partner-provided / unknown) | `non_first_party_members` |

Only `CustomerMatchOrigin.FIRST_PARTY` (collected directly from the data subject by the advertiser)
qualifies. A **purchased** (bought) list and a **discovered** (scraped/enriched) contact are explicitly
prohibited — a single non-first-party member blocks the whole audience.

### B) Account / permission conditions for upload — **separate** from member origin

| Gate | Input | Blocks when | Reason code |
|------|-------|-------------|-------------|
| Policy | `CustomerMatchAccount.customer_match_policy_accepted` | the Customer Match data policy is not accepted | `customer_match_policy_not_accepted` |
| Good standing | `CustomerMatchAccount.account_in_good_standing` | the account is not policy-compliant / eligible | `account_not_in_good_standing` |
| Terms | `CustomerMatchAccount.terms_accepted` | the account terms are not accepted | `terms_not_accepted` |

`ELIGIBLE` requires **both families** to pass. Every failing gate is collected (not short-circuited) in
a fixed order — data-origin family first, then the account-permission family — and each blocker carries
its `family` so a reviewer can see the separation.

### Origin is preserved through hashing

Customer Match matches on **hashed** identifiers (`hash_member` normalizes an email to lowercase/trim
or a phone to E.164, then SHA-256-hashes it). Hashing transforms the *identifier*, never the
*provenance*: the member's `origin` is carried through onto the `HashedMember` unchanged. So a
bought/discovered contact is **still bought/discovered after it is hashed**, and the data-origin gate
rejects it exactly the same — identified in the rejection by its **hash only** (the raw value never
surfaces). Hashing can never launder a non-first-party origin into an uploadable one.

### Load-bearing invariants (acceptance criteria)

- **Bought/discovered contacts are rejected even when hashed or "accepted" by the client.** The
  data-origin gate reads each member's preserved `origin` (surviving hashing untouched) and blocks the
  audience when any member is not first-party. A client self-attestation
  (`CustomerMatchEligibilityRequest.client_attested_first_party`) is **recorded for audit but never
  consulted** — a client cannot vouch a purchased list into eligibility.
- **The account family is real and distinct.** A fully first-party audience is still `BLOCKED` when the
  policy/good-standing/terms conditions are unmet (`data_origin_clear = True`,
  `account_conditions_met = False`) — proving the account gate denies independently of member origin.
- **Pure and reproducible.** The decision reads nothing and writes nothing; a given `(request, now)`
  always yields the identical result — the "changing no state" criterion holds intrinsically.

`ELIGIBLE` is **not** recipient consent and **not** an upload authorization. An actual Customer Match
upload stays a disabled future capability (its own story, with source-rights + rulepack + rights
review). `permits_channel` is Customer-Match-only; `grants_consent` is always `False`.

## Public surface

- `decide_customer_match_eligibility(request, *, now)` — the pure composition over
  `CustomerMatchEligibilityRequest` (members + account + the recorded-only client attestation).
- `evaluate_customer_match_eligibility(...)` — convenience that hashes raw
  `(key_type, raw_value, origin)` members (exactly as an upload would) then decides.
- `hash_member(...)` / `build_audience(...)` — normalize + SHA-256-hash, preserving origin.
- `origin_is_first_party(origin)`.
- Value objects: `HashedMember`, `CustomerMatchAccount`, `CustomerMatchEligibilityRequest`,
  `CustomerMatchEligibilityDecision`, `RejectedMember`, `EligibilityBlocker`.
- Enums: `CustomerMatchOrigin`, `MemberKeyType`, `CustomerMatchEligibility`; the stable
  `REASON_*` / `FAMILY_*` codes and `CUSTOMER_MATCH_CHANNEL` / `HASH_ALGORITHM`.

## Tests

`tests/test_customer_match_eligibility.py` pins the behaviour: the reproducible `ELIGIBLE`; the
acceptance cases (a purchased contact rejected after hashing, identified only by its hash; a discovered
contact rejected after hashing; a client attestation that cannot vouch a purchased list; unknown origin
fail-closed; empty audience blocked); that hashing preserves origin while collapsing the same raw value
to the same hash; the separate-and-real account family (policy/good-standing/terms, reported in order);
the fixed family order of collected blockers; Customer-Match-only channel scoping; the convenience and
`build_audience` paths; and reproducibility.

The evaluator is a **pure function of structured inputs** — it opens no DB, calls no adapter and
consults no model — so there is no persisted read/write path to exercise. The "changing no state"
acceptance criterion holds intrinsically: nothing is read or written, so no disposable-PostgreSQL
harness is required for this story.

## Manual check

```
python scripts/manual_check.py DCA-091
```

Runs, in isolated synthetic Businesses A/B:

1. an all-**first-party** audience with an eligible account → `ELIGIBLE`;
2. the same audience with one **purchased** contact added, **hashed identically** and **client-attested**
   → `BLOCKED` on `non_first_party_members` (attestation recorded, never consulted; the bought contact
   named only by its SHA-256 hash, which matches `sha256('bought@list.example')`);
3. a **discovered** (scraped) phone contact → `BLOCKED` on `non_first_party_members` after hashing;
4. a fully first-party audience with the Customer Match **policy not accepted** → `BLOCKED` on
   `customer_match_policy_not_accepted` (`data_origin_clear = True`, account family denies separately).

Local synthetic only; no AI, no paid action, no network, no upload; the decision changes no state.
```
