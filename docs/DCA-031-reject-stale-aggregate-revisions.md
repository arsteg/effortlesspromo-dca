# DCA-031 — Reject stale aggregate revisions

**Status:** implemented. Adds **revision-aware projection processing** to the inbound
integration-event path: an event that projects an aggregate's state is applied **only in revision
order**, so a delayed or retried delivery carrying an **older** revision can never overwrite a newer
projection, and a revision that arrives **ahead of a missing predecessor** is held in a visible,
reconcilable buffer rather than applied over a gap. No remote I/O, no other service, no UI, no
AI-service internals. Reuses the DCA-029 envelope's existing `aggregate_version`; it does **not**
change the envelope contract. Not wired into any live inbound route — a ready, independently
reviewable capability. Future capabilities stay disabled.

## What it does

`app/projection/revision.py` defines `RevisionProjector` — the single sanctioned writer of the two
new tenant-owned tables (`app/db/models.py`):

| Table | Purpose |
|---|---|
| `revision_projections` | one row per aggregate `(business_id, aggregate_type, aggregate_id)` holding the highest **contiguous** `applied_version`, its opaque projected `state`, and a `current`/`gapped` status |
| `revision_pending_events` | out-of-order revisions buffered ahead of a missing predecessor, unique per `(business_id, aggregate_type, aggregate_id, aggregate_version)` so a re-delivery is not double-buffered |

`RevisionProjector.apply(...)` decides exactly one outcome and stages the matching change into the
**caller's** session (it never commits, flushes a transaction of its own, opens one, or does any
I/O — the caller owns the unit of work, exactly like the DCA-026/027/028 sinks):

- **APPLIED** — the event is the next contiguous revision (`version == applied_version + 1`, or the
  very first at `version == 1`). Its state becomes the projection, `applied_version` advances, and
  any buffered successor revisions that are now contiguous are **reconciled** (applied in order, then
  removed from the buffer).
- **STALE** — the event's revision is `<= applied_version`: an older (or duplicate) revision. It is
  refused and **nothing is mutated** — an older consent/profile revision can never overwrite the
  current one, and the denial is reproducible with no state change.
- **GAP** — the event's revision is `> applied_version + 1`: the revisions in between are missing and
  their state is unknown, so the event is **not applied over the gap**. It is buffered so the gap is
  **visible** (`pending(...)` lists the buffered revisions; `gaps(...)` computes the still-missing
  versions — never guessed), and the projection is marked `gapped`. Once the missing revision
  arrives, the buffered events reconcile in order.

The aggregate identity (`aggregate_type` / `aggregate_id`) is supplied by the caller — a guarded
receiver that decides which aggregate an event projects — so the DCA-029 envelope (frozen,
`extra="forbid"`) is untouched.

### Concurrency (why it needs PostgreSQL)

The projection row is read `SELECT … FOR UPDATE`, so two concurrent deliveries for the **same**
aggregate serialize: the second waits, then reads the first's committed advance and cannot lose an
update or resurrect a stale revision. `FOR UPDATE` is a no-op on SQLite, so the concurrency behaviour
is verified on a **disposable PostgreSQL**, never SQLite.

### No raw secrets or contact data

`aggregate_type` / `aggregate_id` / `event_id` and every `state` value are screened by the shared
DCA-012 redaction policy (via the audit screens) **before** anything is staged; a value carrying an
email, a phone number, a registered secret, or a sensitive key is refused with `RevisionPayloadError`
and no row is created. Candidate acceptance is not recipient consent and the projection holds no
contact data.

### Tenant isolation

Both tables are `TenantOwned`, so they are automatically in `tenant_owned_tables()` and covered by
the DCA-018 row-level-security policy like every other tenant data table — one tenant can never read
another's projections. Every projector read/write is confined to the verified context's business.
Migration `0008_revision_projections` adds both tables + their RLS policies for databases that
predate them (on a fresh DB the metadata baseline + 0002 already cover it; 0008 is a guarded no-op —
same dual-path style as 0005/0006/0007).

## Tests

`tests/test_revision_projection.py`, two layers:

```
# always-on unit tests (no database) — the decision logic + redaction + staging discipline:
pytest tests/test_revision_projection.py

# FOR UPDATE row-lock serialization + real-Postgres stale rejection (disposable PostgreSQL):
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55476:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55476/dca"
pytest tests/test_revision_projection.py tests/test_migrations.py tests/test_rls.py
```

The unit layer pins: the first revision applies; contiguous revisions advance in order; a delayed
older revision (apply 3 then 2) is **STALE with no overwrite**; a duplicate of the current revision
is a stale no-op; a revision ahead of a missing predecessor is **buffered as a visible GAP** and the
projection is **not** advanced; the gap **reconciles in order** once the missing revision arrives; a
first event already ahead of revision 1 is a gap from the start; a re-delivery of a buffered revision
is not double-buffered; projections are tenant-isolated; `apply` stages without committing; non-opaque
identity/state is refused with no row staged; invalid scope/type/id/version raise; and a stale apply
stages/mutates nothing. The Postgres layer proves the stale rejection on real Postgres and that two
interleaved deliveries (revision 2 and 3) serialize on the row lock so the final projection is the
highest contiguous revision (3) with revision 3's state — no lost update, no stale winner. Migration
coverage: `tests/test_migrations.py` adds both tables to the expected set and a 0008 create-branch
test; `tests/test_rls.py` asserts RLS covers both new tables.

Quality gate (all clean): `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```
python scripts/manual_check.py DCA-031
```

For synthetic Business A, a guarded receiver mints+validates a DCA-029 envelope and projects its
revision: (1) revisions 1→2→3 apply in order; (2) a **delayed revision 2** is rejected STALE and the
state stays at revision 3 (unchanged); (3) revision 5 while 4 is missing is **buffered as a visible
GAP** (`pending=[5]`, `missing_gap=[4]`), not applied; (4) the missing revision 4 fills the gap and
reconciles 5 in order (`applied_version=5`, `status=current`, buffer drained). It prints the actual
projection row, buffered pending rows and computed missing versions at each step. Local synthetic
SQLite only; no AI/paid action; the ephemeral DB is cleaned up.

## Boundary

Inside the DCA only: two new models + their `tenant_unique` constraints, migration
`0008_revision_projections`, `app/projection/` (`RevisionProjector` + outcome/errors), focused tests,
the two tenant-owned-table assertions updated (`tests/test_rls.py`, `tests/test_migrations.py`), this
doc, and the DCA-031 manual-check scenario. No change to the DCA-029 envelope or any other service,
no UI, no AI-service internals. The projector is **not** wired into the sinks or any live inbound
route — that remains a separate, disabled capability.
