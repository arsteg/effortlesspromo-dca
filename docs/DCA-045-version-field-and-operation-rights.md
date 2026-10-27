# DCA-045 — Version field and operation rights

Stage: Source rights and policy foundation · key: `source-rights`

## What this adds

Versioned **source terms** — for a catalogued `Source` (DCA-044), a reviewable record of *what the
source's contract/licence permits*, expressed **per field and per operation** rather than as one
legal boolean. Each save is a new **version**; rights carry an **explicit validity window**.

A `SourceTerms` row (one version of one source's terms) records:

- **operation rights** — the source-wide default grant for each of the seven operations
  (`collect` | `store` | `derive` | `display` | `export` | `share` | `model_processing`).
  **Deny-by-default**: an operation absent from the input is stored explicitly as *not granted*.
- **field rights** — per-field OVERRIDES (`{field: {operation: granted}}`). A field named here
  overrides the source-wide default for exactly the operations it lists, so **one field can be
  display-only while another is also exportable**; a field not named inherits the defaults.
- **purposes** — the purposes the rights are bound to (rights are purpose-limited; ≥1 required).
- **attribution** — `{"required": bool, "notice": str}` the source requires.
- **retention** — `{"max_days": int|None, "policy": str}` (a negative/non-integer window is refused).
- **contract evidence** — `{"reference", "kind", "uri"}`; a terms version **cannot be staged without
  a non-empty `reference`** (a bare rights claim is refused).
- **effective_from / expires_at** — the EXPLICIT validity window; an expired or not-yet-effective
  version grants nothing.

### The guarantees (acceptance criteria)

- **Rights can differ per field/use; expiry and version are explicit — never one boolean.**
  `SourceTermsStore.permits(terms, field, operation, now=…)` is a pure, side-effect-free read that
  resolves the field override first, then the source-wide default (deny-by-default), and honours the
  validity window. The result (`RightDecision`) records `allowed`, a stable `reason` on denial, and
  whether the grant came from a `field_override` or the `operation_default`.
- **Reproduce the result and the relevant denial without unintended state changes.** The headline
  denial — `export` on a display-only field — and the **expired-version** denial are both pure reads
  that change nothing. Staging refusals (uncatalogued source, unknown operation, missing contract
  evidence / purpose, bad retention, incoherent window) are raised **before** any row is staged.
- **Versions are insert-only and preserved.** `add_version` inserts at `max(version)+1`; a prior
  version is never overwritten, so rights history is reviewable version-by-version.
- **Future capabilities stay disabled.** Every row is `status='draft'`; `is_active` is always `False`.
  Recording rights does not put them in force and does not enable the source — activating a terms
  version and enabling a source (DCA-044) remain separate, disabled stories.

## Components touched (DCA only)

- **Model**: `app/db/models.py` → `SourceTerms` + `SourceOperation` / `SourceTermsStatus`. Platform
  **control-plane** data (a shared registry), like `sources` (DCA-044) / `tenant_agents` —
  deliberately **outside tenant RLS** and never tenant-scoped.
- **Store**: `app/services/source_terms.py` → `SourceTermsStore`, `SourceTermsInput`, `RightDecision`,
  validation + stable reason codes, the `permits` field/use/expiry evaluator.
- **Migration**: `migrations/versions/0017_source_terms.py` (dual-path, mirrors 0016; no RLS policy).
- **Manual check**: `scripts/manual_check.py` → `DCA-045` scenario.

No HTTP surface, Web/Admin UI, or AI-service change is in scope: this is the schema + store. A
platform-operator surface to author/activate terms is a later story, kept disabled here ("missing
contracts stay disabled").

## Stable codes

Staging refusals (raised by `add_version` before any row is staged): `source_not_found`,
`unknown_operation`, `missing_contract_evidence`, `missing_purpose`, `invalid_retention`,
`invalid_expiry`. Rights-evaluation denials (returned by `permits`): `operation_not_permitted`,
`terms_expired`, `terms_not_yet_effective`.

## Tests

- `tests/test_source_terms.py` — the behaviour + failure cases (rights differ per field/use,
  deny-by-default operation rights, insert-only versioning, expiry & not-yet-effective denials, a
  recorded version is never active, and every staging refusal leaves no row).
- `tests/test_migrations.py` — `source_terms` added to the DCA table set;
  `test_0017_creates_source_terms_on_a_preexisting_database` (disposable Postgres) proves the
  dual-path create + unique `(source_key, version)` constraint and **no** tenant RLS policy.
- `tests/test_rls.py` — asserts `source_terms` is excluded from the tenant-owned RLS set (control-plane).
- `tests/test_manual_check.py` — runs the `DCA-045` scenario end-to-end on synthetic SQLite.

## Developer manual test

```
python scripts/manual_check.py DCA-045
```

Expected: a source (`mca_master_data`) is catalogued and a terms **version 1** recorded whose rights
differ per field/use — `email` is display-only (`display` ALLOW, `export` DENY `operation_not_permitted`)
while `company_name` is exportable (`display` + `export` ALLOW); the version prints an explicit
`effective_from`/`expires_at`. The per-field/per-use denial and the **expired-version** denial
(`export` on `company_name` after `expires_at` → `terms_expired`) are reproduced as pure reads. An
uncatalogued source (`source_not_found`), an unknown operation (`unknown_operation`), and missing
contract evidence (`missing_contract_evidence`) are each refused, leaving the registry at **1 row**.
Local synthetic only; no AI/paid action; nothing is activated or enabled.

To exercise the DDL/RLS paths on disposable PostgreSQL (not SQLite):

```
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
pytest tests/test_migrations.py tests/test_rls.py
```
