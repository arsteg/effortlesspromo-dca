# DCA-063 — Filter fields by display entitlement

Stage: Evidence and identity resolution · key `field-visibility` · source sections 6, 7.

## What this adds

A **rights-aware projection** applied to record/evidence API responses. A licensed shared fact (a
field observed from a catalogued source) is served back to a business **only when that business holds
a DISPLAY entitlement for it**; a restricted value is **absent from the payload**, not merely hidden
by a consumer. A private overlay — the business's own un-sourced data — is always visible to its owner.

- Projection `app/projection/field_visibility.py` — pure, side-effect-free: `project_field` /
  `project_fields` / `project_evidence`, the `DisplayDecider` port, and `display_decider_from_permits`
  adapting the DCA-047 `permits(..., "display")` read.
- Route `app/api/routes/records.py` — read-only HTTP contract (registered in `app/main.py`):
  - `GET /v1/tenants/{business_id}/records/{subject_type}/{subject_id}` — a subject's projected fields.
  - `GET /v1/tenants/{business_id}/evidence/{evidence_id}` — one projected evidence object.

No new model, no migration (reads the existing DCA-047/061/062 tables), no AI capability, no
acquisition/send path. No .NET / UI / AI-service changes. Future capabilities stay disabled.

## The projection (deny-by-default)

For each field observation:

- **Private overlay** — no originating `source_key` (the business derived it itself). Not licensed
  from any source → **always visible** to the owning business.
- **Licensed shared fact** — names a `source_key`. Visible only when
  `BusinessSourceEntitlementStore.permits(source_key, field_key, "display")` allows it — the
  intersection of the platform ceiling (in-force DCA-046 approval) and the business's own DCA-047
  narrowing. When display is **not** entitled the value is dropped (`value = None`, never serialized)
  and a stable `restricted_reason` is recorded (`operation_not_entitled`, `source_not_entitled`,
  `source_not_platform_approved`, `operation_denied_at_platform`, …). Provenance (key/source/scope)
  stays so the denial is reviewable without leaking the value.

Evidence: the raw payload is carried only when display is entitled for `(source_key, content_kind)`
**and** the payload was retained (a DCA-062 `reference_only` object holds none). The locator / digest /
retention-mode metadata stays visible as traceability.

### Serialization — the value is absent, not nulled

The route serializer includes the `value` key **only** for a visible field; a restricted field is
serialized with `"restricted": true` + `"restrictedReason"` and **no** `value` key. Evidence mirrors
this with `rawContent` / `rawRestricted` / `rawRestrictedReason`.

## Acceptance criteria → evidence

- **Restricted values are absent from payloads, not merely hidden in the UI.** `project_field` drops
  the value (`value is None`) for an unentitled licensed fact and `_field_view` omits the key; the raw
  response body does not contain the value.
  (`test_field_visibility_projection.py::test_licensed_fact_value_absent_when_display_not_entitled`,
  `test_records_api.py::test_unentitled_business_the_value_is_absent_from_the_payload` — asserts the
  value string is not anywhere in `resp.text`.)
- **Reproduce the result and the denial without unintended state changes.** The routes and the
  projection are read-only — nothing is written. Two businesses observe the SAME source-licensed field
  with different display rights; comparing the raw JSON shows A's value present and B's absent
  (`test_records_api.py::test_raw_json_differs_only_by_the_restricted_value`), and the manual-check
  re-projects B's record to confirm the field-row count is unchanged
  (`manual_check.py DCA-063` → `projection_is_read_only`).
- Supporting: private overlays visible to both regardless of entitlement
  (`test_private_overlay_is_visible_to_both_regardless_of_entitlement`); evidence raw payload gated the
  same way with metadata kept (`test_evidence_raw_payload_is_gated_by_display_entitlement`); tenant
  isolation — B cannot read A's subject or evidence (404)
  (`test_tenant_isolation_cross_business_subject_is_404`).

## Developer manual test

```text
# focused unit + route tests (in-memory SQLite, no network/Docker)
pytest tests/test_field_visibility_projection.py tests/test_records_api.py

# inspectable scenario (local synthetic, ephemeral DB, AI disabled) — prints the A-vs-B raw state
python scripts/manual_check.py DCA-063

# the same A-vs-B projection on the real transaction/RLS engine (disposable PostgreSQL, NOT SQLite)
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55490:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55490/dca"
pytest tests/test_records_api.py
```

Expected (`DCA-063` manual check): Business A (display-entitled) sees `revenue_value = 120000000`;
Business B (not entitled) shows `revenue_value_is_absent = True`,
`revenue_restricted_reason = operation_not_entitled`; both see their own `segment` overlay; evidence
raw payload visible for A, absent for B with the digest preserved for both; `no_state_change = True`.

Manual sign-off stays with the developer.
