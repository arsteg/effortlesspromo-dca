# DCA-094 — Issue short-lived activation references

Stage: India channel eligibility · key `activation-reference` · source sections 5, 10.

## What this adds

A tamper-resistant, short-lived **activation reference**: a signed proof that one of a business's own
contact points cleared the DCA's compliance gates for a specific channel at a specific moment. It turns
a *definitive ELIGIBLE* answer from the DCA-093 eligibility evaluator into a portable token that a later
consumer can validate offline and bind to the exact context it was issued for.

A reference is **not** send permission. It proves the DCA's own blockers were clear for exactly this
context when issued, and nothing more — it is not recipient consent and not a send authorization. The
one send pipeline still owns and re-gates the actual send, which stays a disabled future capability.

## Design

- **Pure mint/verify pair** — `app/activation/activation_reference.py`, the same shape as the DCA-076
  withdrawal token. HMAC-SHA256 over `<version>.<payload>`; constant-time compare. Self-contained, so
  no server-side token store, no table, no migration.
- **Binds the whole activation context** into the signed payload: `business_id` (tenant), the recipient
  (contact-point id **+ a one-way SHA-256 digest** of the value — never the raw value), `channel`,
  `purpose`, `provider`, `campaign_revision`, and the `permission_version` + `source_version` the
  decision rested on. Change any dimension and the signature no longer matches.
- **Short-lived** — `iat`/`exp` epoch seconds in the payload; refused at/after `exp`
  (`EFFORTLESSPROMO_DCA_ACTIVATION_REFERENCE_TTL_SECONDS`, default 900s / 15 min).
- **Intended-context re-assertion** — a verifier may pass the context it is about to act on
  (`expected`); any non-empty dimension that disagrees with the signed reference is rejected, so a
  caller that lies about the recipient/channel is refused even with an authentic token.
- **Fail-closed** — no signing secret configured ⇒ the whole capability is disabled (no reference is
  minted, every presented one is refused). Secret is distinct from every auth/service/webhook/withdrawal
  secret; value arrives out-of-band (`*_secret` / `*_secret_ref`), same stance as every DCA secret.

## HTTP contract (`app/api/routes/activation_references.py`, `X-Service-Auth`)

- `POST /v1/tenants/{business_id}/activation-references/{channel}` — evaluate this business's OWN
  contact point (reusing the DCA-093 pipeline: tenant-scoped resolution → pure email evaluator →
  three-state fold). A definitive **ELIGIBLE** returns `issued: true` + the `reference` + its `binding`
  (recipient as a digest) + expiring metadata + evidence/version references. **REVIEW/BLOCKED** returns
  `200` with `issued: false`, `reference: null` and the outcome/reasons — nothing is minted. A
  cross-tenant/unknown contact reads as `404`. Only `email` is wired; other known channels are
  `channel_not_enabled`.
- `POST /v1/tenants/{business_id}/activation-references/{channel}/verify` — validate a presented
  reference (optionally against an `expected` context). The path business is always asserted against the
  signed binding. Rejections are `422` whose message is the stable reason code (with the offending
  dimension appended on a binding mismatch, e.g. `activation_reference_binding_mismatch:channel`).

Both routes are read-only (they write nothing), so every result and denial reproduce with no state
change.

## Rejection reason codes

`activation_reference_no_secret`, `activation_reference_invalid_format`,
`activation_reference_unsupported_version`, `activation_reference_bad_signature`,
`activation_reference_malformed_payload`, `activation_reference_expired`,
`activation_reference_binding_mismatch` (with the offending dimension).

## Acceptance evidence

- **An altered or expired reference is rejected** — altered recipient, altered channel, tampered
  signature, wrong tenant, and expired are each refused with a stable reason (manual check steps 2–4,
  `tests/test_activation_reference*.py`).
- **A reference is not permanent send permission** — short-lived (expires), and every response carries
  `activation.authorizesSend == False`; a REVIEW/BLOCKED outcome mints no reference at all.
- **Reproducible, no state change** — re-issuing produces the identical binding; verification reads and
  writes nothing.

## Run it

```text
pytest tests/test_activation_reference.py tests/test_activation_reference_api.py
python scripts/manual_check.py DCA-094
```

The PostgreSQL isolation test in `tests/test_activation_reference_api.py` runs only when
`DCA_TEST_DATABASE_URL` points at a disposable Postgres; it skips otherwise.

## Boundary

DCA-only (Python). No AI-service, Web, Admin, migration, or .NET change. Acting on a reference (an
actual send) stays a disabled future capability behind the one send pipeline.
