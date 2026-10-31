# DCA-143 — Convert one staged task into observations

Stage 13 (Discovery pipeline in small steps). Key: `extract-stage`. Source sections 4, 8.

## What this adds

The next step of the still-disabled discovery pipeline after DCA-142 executes a task. DCA-142 runs the
DCA-123 organization-dataset adapter for one approved, frozen task and stages a **permitted raw
checkpoint** (`staged_result`) on a `DiscoveryTaskExecution`. DCA-143 **converts** one such *DONE* task
into normalized, provenanced **observations**:

- reads the organization identities the task imported (`OrgObservation` + `OrgExternalId`, via the
  DCA-123 `OrgObservationImport` provenance rows the staged checkpoint references);
- captures the checkpoint as a license-aware **evidence object** (DCA-062) — the grounding every fact
  cites; retention is **derived from the cited source terms** (a no-retention source keeps only the
  reference + digest, no raw copy);
- records each **permitted** organization-level field as an **observed field observation** (DCA-061)
  that cites that evidence, honoring the required-provenance rules of the field store.

It connects deterministic parsing to observation validation/storage, preserving source evidence and task
idempotency. There is **no network fetch, no model call, no spend and no send** — it reads
already-collected, already-permitted data and runs the two sanctioned observation writers.

## Boundary

Python DCA only (`apps/effortlesspromo-dca`). No AI-service, Web, Admin, or .NET change. No new source is
enabled, no live adapter ships, nothing is sent or charged. Acceptance of a prospect is not recipient
consent; no platform export exists.

## Permitted-field allowlist (forbidden fields never persist)

`PERMITTED_OBSERVATION_FIELDS = {legal_name, description, industry, cin, pan, gstin}` — all
organization-level; the list contains **no contact channel**. Candidate fields are derived from each
identity (display name → `legal_name`; each external register id keyed by its scheme). Any candidate not
on the allowlist — e.g. a valid-but-non-permitted external-id scheme such as `duns`/`lei`/`tan` — is
**dropped** (counted in `fields_dropped`), never written.

## Guards (fail-closed, in order)

1. **Authority FIRST** — the caller must hold `run.plan.approve` (the same chargeable-discovery
   authority that approved + executed the task); else `TaskConversionAuthorizationError`
   (`task_conversion_not_authorized`). Checked before any state is read.
2. **Exists in this business** — the run + the task's execution must exist within this business; an
   unknown or cross-tenant id reads as not-found (`discovery_task_execution_not_found`).
3. **Convertible state** — only a `DONE` execution (which staged a result) is convertible; a
   `DENIED`/`FAILED` task is refused (`discovery_task_not_convertible`).
4. **Idempotent replay** — `(business_id, run_id, task_index)` is unique on
   `DiscoveryTaskConversion`; a second convert returns the existing record and writes nothing new.
5. **Normalize** — resolve the source terms version (from the business entitlement), capture the
   checkpoint evidence, and record each permitted field (dropping the rest).

Every refusal is raised before the session is mutated, so a denial changes nothing. The converter stages
into the caller's unit of work and never commits; the conversion record, the evidence, the field
observations and their atomic DCA-026 audit entries commit together.

## Changed / added files

- `app/services/discovery_observation.py` — new `DiscoveryObservationConverter` + `TaskConversionActor`
  / `TaskConversionResult` / errors + the permitted-field allowlist.
- `app/db/models.py` — new tenant-owned `DiscoveryTaskConversion` model (RLS, one per `(business, run,
  task_index)`).
- `migrations/versions/0062_discovery_task_conversions.py` — dual-path create + RLS policy/grant
  (mirrors 0061).
- `tests/test_discovery_observation.py` — focused service tests (SQLite) + a Postgres-backed test.
- `tests/test_migrations.py` — `discovery_task_conversions` added to `DCA_TABLES`; a `0062` create-branch
  test.
- `tests/test_rls.py` — `discovery_task_conversions` added to the tenant-owned RLS coverage set.
- `scripts/manual_check.py` — `@register("DCA-143", …)` inspectable scenario.
- `docs/DCA-143-convert-one-staged-task-into-observations.md` — this note.

## Verification

```text
# focused (SQLite; the Postgres-backed case skips without a URL)
.venv/Scripts/python.exe -m pytest tests/test_discovery_observation.py -q

# quality gate
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy

# inspectable scenario (prints the staged result, the recorded observations, the dropped
# forbidden field, the captured evidence + retention, an idempotent re-convert, and a
# DENIED-task refusal)
.venv/Scripts/python.exe scripts/manual_check.py DCA-143

# disposable Postgres (locking / RLS / transactions — not SQLite):
#   export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
.venv/Scripts/python.exe -m pytest tests/test_discovery_observation.py tests/test_migrations.py tests/test_rls.py -q
```

## Status

**Ready for manual review.** Not committed; human acceptance not marked. Stops here — do not start DCA-144.
